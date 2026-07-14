"""
Milestone 5: Flow topology tests using a fully stubbed worker.

These tests exercise the @router, revise loop, and cap logic without
touching any real CLI or network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from crewai_headless_flow.config import FlowConfig
from crewai_headless_flow import flow as flow_module
from crewai_headless_flow.flow import CrewAIHeadlessFlow
from tests.flow_test_helpers import patch_build_headless_flow
from crewai_headless_flow.review_contract import REVIEW_DECISION_SCHEMA, ReviewDecision
from crewai_headless_flow.review_crew import ReviewCrewDecision
from crewai_headless_flow.state import (
    AbortedCheckpoint,
    FlowState,
    HumanFeedbackEntry,
    TaskItem,
)
from crewai_headless_flow.workers import ClaudeAdapter, CursorAdapter, GeminiAdapter
from crewai_headless_flow.workers.base import CoderResult


pytestmark = pytest.mark.offline


class StubWorker:
    """Minimal stub that returns controllable results for testing the Flow logic."""

    def __init__(
        self,
        review_outcome: str = "pass",
        issues: list[str] | None = None,
        raw_review_output: str | None = None,
    ):
        self.review_outcome = review_outcome
        self.issues = issues or []
        self.raw_review_output = raw_review_output
        self.call_count = 0

    def run(self, task: str, cwd: str, mode: str = "edit", **kwargs):
        self.call_count += 1

        if "review" in task.lower() or mode == "inspect":
            # Simulate review worker output
            if self.raw_review_output is not None:
                raw = self.raw_review_output
            elif self.review_outcome == "pass":
                raw = '{"status": "pass", "issues": [], "summary": "Looks good"}'
            else:
                raw = f'{{"status": "revise", "issues": {self.issues}, "summary": "Needs work"}}'

            return CoderResult(
                summary="review complete",
                raw_output=raw,
                exit_code=0,
            )

        # do_work / finalize simulation
        return CoderResult(
            summary="work done",
            changed_files=["src/example.py", "tests/test_example.py"],
            raw_output="Implemented the requested change.",
            exit_code=0,
        )


class RecordingWorker:
    def __init__(self, inspect_raw: str | None = None):
        self.calls: list[dict] = []
        self.inspect_raw = inspect_raw or (
            '{"status": "pass", "issues": [], "summary": "Looks good"}'
        )

    def run(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("mode") == "inspect":
            return CoderResult(
                summary="review complete",
                raw_output=self.inspect_raw,
                exit_code=0,
            )
        return CoderResult(
            summary="work complete",
            changed_files=[],
            raw_output="work complete",
            exit_code=0,
        )


class FinalizeOnlyAdapter:
    calls: list[dict[str, Any]] = []

    def run(self, task: str, cwd: str, mode: str = "edit", **kwargs):
        self.__class__.calls.append({"task": task, "cwd": cwd, "mode": mode, **kwargs})
        return CoderResult(
            summary="finalized",
            changed_files=[],
            raw_output="finalized",
            exit_code=0,
        )


class MutatingWorker:
    def __init__(self, task_writes: dict[int, dict[str, str]]):
        self.task_writes = task_writes
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        task_id = _extract_task_id(kwargs["task"])
        cwd = Path(kwargs["cwd"])
        for rel_path, content in self.task_writes[task_id].items():
            target = cwd / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return CoderResult(
            summary=f"task {task_id} complete",
            changed_files=[],
            raw_output=f"task {task_id} complete",
            exit_code=0,
        )


class PlanningMutatingWorker:
    def __init__(
        self, plan_summary: str, plan_tasks: list[dict[str, Any]], task_writes
    ):
        self.plan_summary = plan_summary
        self.plan_tasks = plan_tasks
        self.task_writes = task_writes
        self.calls: list[dict] = []

    def run(self, *args, **kwargs):
        if args and "task" not in kwargs:
            kwargs["task"] = args[0]
        self.calls.append(kwargs)
        if kwargs.get("mode") == "inspect":
            return CoderResult(
                summary=json.dumps(
                    {
                        "summary": self.plan_summary,
                        "tasks": self.plan_tasks,
                    }
                ),
                raw_output="",
                exit_code=0,
            )

        task_id = _extract_task_id(kwargs["task"])
        cwd = Path(kwargs["cwd"])
        for rel_path, content in self.task_writes[task_id].items():
            target = cwd / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return CoderResult(
            summary=f"task {task_id} complete",
            changed_files=[],
            raw_output=f"task {task_id} complete",
            exit_code=0,
        )


class FailingMutatingWorker:
    def __init__(
        self,
        *,
        failing_tasks: dict[int, tuple[dict[str, str], str]],
        success_writes: dict[int, dict[str, str]],
    ):
        self.failing_tasks = failing_tasks
        self.success_writes = success_writes
        self.failed_once: set[int] = set()
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        task_id = _extract_task_id(kwargs["task"])
        cwd = Path(kwargs["cwd"])
        if task_id in self.failing_tasks and task_id not in self.failed_once:
            writes, error = self.failing_tasks[task_id]
            self.failed_once.add(task_id)
            for rel_path, content in writes.items():
                target = cwd / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            return CoderResult(
                summary=error,
                changed_files=[],
                raw_output=error,
                exit_code=1,
                error=error,
            )

        for rel_path, content in self.success_writes[task_id].items():
            target = cwd / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return CoderResult(
            summary=f"task {task_id} complete",
            changed_files=[],
            raw_output=f"task {task_id} complete",
            exit_code=0,
        )


def _extract_task_id(prompt: str) -> int:
    for line in prompt.splitlines():
        if line.startswith("- Id: "):
            return int(line.removeprefix("- Id: ").strip())
    raise AssertionError(f"Prompt did not include task id:\n{prompt}")


def _make_repo(root: Path, files: dict[str, str]) -> Path:
    for rel_path, content in files.items():
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return root


def _run_do_work(flow: CrewAIHeadlessFlow, plan_output: str) -> str:
    return cast(Any, flow).do_work(plan_output)


def test_flow_builds_claude_worker_from_worker_config():
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"do_work": {"worker": "claude"}},
        defaults={"worker": "codex", "timeout": 300},
    )

    flow = CrewAIHeadlessFlow(config=cfg)

    assert isinstance(flow._get_worker("do_work").worker, ClaudeAdapter)


def test_flow_builds_gemini_worker_from_worker_config():
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"do_work": {"worker": "gemini"}},
        defaults={"worker": "codex", "timeout": 300},
    )

    flow = CrewAIHeadlessFlow(config=cfg)

    assert isinstance(flow._get_worker("do_work").worker, GeminiAdapter)


def test_flow_builds_cursor_worker_from_worker_config():
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"do_work": {"worker": "cursor"}},
        defaults={"worker": "codex", "timeout": 300},
    )

    flow = CrewAIHeadlessFlow(config=cfg)

    assert isinstance(flow._get_worker("do_work").worker, CursorAdapter)


def test_flow_rejects_unknown_worker_name():
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"do_work": {"worker": "claude-typo"}},
        defaults={"worker": "codex", "timeout": 300},
    )

    with pytest.raises(
        ValueError,
        match="Unsupported worker 'claude-typo' configured for stage 'do_work'",
    ):
        CrewAIHeadlessFlow(config=cfg)


def test_do_work_passes_configured_model_to_worker():
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"do_work": {"worker": "claude", "model": "sonnet"}},
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    worker = RecordingWorker()
    flow._workers["do_work"] = worker  # type: ignore

    flow.do_work("plan")

    assert worker.calls[0]["model"] == "sonnet"


def test_do_work_tracks_detected_file_changes_when_worker_reports_none(
    tmp_path: Path,
):
    repo = _make_repo(tmp_path / "repo", {"src/a.py": "before\n"})
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"do_work": {"worker": "claude", "model": "sonnet"}},
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"])
    ]
    worker = MutatingWorker({1: {"src/a.py": "after\n"}})
    flow._workers["do_work"] = worker  # type: ignore

    result = _run_do_work(flow, "plan output")

    assert worker.calls[0]["cwd"] == str(repo)
    assert flow.state.tasks[0].status == "done"
    assert flow.state.changed_files == ["src/a.py"]
    assert flow.state.history[-1].files == ["src/a.py"]
    assert (repo / "src/a.py").read_text() == "after\n"
    assert "Task 1 complete" in result


def test_do_work_parallel_path_uses_isolated_workspaces_and_merges_disjoint_changes(
    tmp_path: Path,
):
    repo = _make_repo(
        tmp_path / "repo",
        {
            "src/a.py": "before a\n",
            "src/b.py": "before b\n",
        },
    )
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={
            "do_work": {
                "worker": "claude",
                "model": "sonnet",
                "parallel": {"enabled": True, "max_workers": 2},
            }
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"]),
        TaskItem(id=2, title="two", description="task two", files=["src/b.py"]),
    ]
    worker = MutatingWorker(
        {
            1: {"src/a.py": "after a\n"},
            2: {"src/b.py": "after b\n"},
        }
    )
    flow._workers["do_work"] = worker  # type: ignore

    result = _run_do_work(flow, "plan output")

    assert len(worker.calls) == 2
    assert all(call["model"] == "sonnet" for call in worker.calls)
    assert all(call["cwd"] != str(repo) for call in worker.calls)
    assert len({call["cwd"] for call in worker.calls}) == 2
    assert all(task.status == "done" for task in flow.state.tasks)
    assert sorted(flow.state.changed_files) == ["src/a.py", "src/b.py"]
    assert all(entry.parallel_batch_id == "b1" for entry in flow.state.task_executions)
    assert all(entry.isolated_workspace is True for entry in flow.state.task_executions)
    assert [entry.kind for entry in flow.state.history] == [
        "task_complete",
        "task_complete",
    ]
    assert flow.state.debug_report is not None
    assert "Task 1 [done]" in flow.state.debug_report
    assert (repo / "src/a.py").read_text() == "after a\n"
    assert (repo / "src/b.py").read_text() == "after b\n"
    assert "Task 1 complete" in result
    assert "Task 2 complete" in result


def test_do_work_parallel_batch_planner_can_expand_beyond_static_file_hints(
    tmp_path: Path,
):
    repo = _make_repo(
        tmp_path / "repo",
        {
            "src/a.py": "before a\n",
            "src/b.py": "before b\n",
        },
    )
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
        },
        workers={
            "do_work": {
                "worker": "claude",
                "model": "sonnet",
                "parallel": {
                    "enabled": True,
                    "max_workers": 2,
                    "planner": {"enabled": True},
                },
            }
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=[]),
        TaskItem(id=2, title="two", description="task two", files=["src/b.py"]),
    ]
    worker = PlanningMutatingWorker(
        plan_summary="Run tasks 1 and 2 together.",
        plan_tasks=[
            {"task_id": 1, "files": ["src/a.py"]},
            {"task_id": 2, "files": ["src/b.py"]},
        ],
        task_writes={
            1: {"src/a.py": "after a\n"},
            2: {"src/b.py": "after b\n"},
        },
    )
    flow._workers["do_work"] = worker  # type: ignore

    result = _run_do_work(flow, "plan output")

    assert len(worker.calls) == 3
    assert worker.calls[0]["mode"] == "inspect"
    assert worker.calls[1]["cwd"] != str(repo)
    assert worker.calls[2]["cwd"] != str(repo)
    assert flow.state.tasks[0].files == ["src/a.py"]
    assert [entry.kind for entry in flow.state.history] == [
        "batch_planning",
        "task_complete",
        "task_complete",
    ]
    assert all(task.status == "done" for task in flow.state.tasks)
    assert sorted(flow.state.changed_files) == ["src/a.py", "src/b.py"]
    assert "Task 1 complete" in result
    assert "Task 2 complete" in result


def test_do_work_parallel_batch_planner_can_expand_partially_filled_static_batch(
    tmp_path: Path,
):
    repo = _make_repo(
        tmp_path / "repo",
        {
            "src/a.py": "before a\n",
            "src/b.py": "before b\n",
            "src/c.py": "before c\n",
        },
    )
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
        },
        workers={
            "do_work": {
                "worker": "claude",
                "model": "sonnet",
                "parallel": {
                    "enabled": True,
                    "max_workers": 3,
                    "planner": {"enabled": True},
                },
            }
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(
            id=2,
            title="broad",
            description="task broad",
            files=["src/a.py", "src/b.py"],
        ),
        TaskItem(id=3, title="a", description="task a", files=[]),
        TaskItem(id=4, title="b", description="task b", files=["src/b.py"]),
        TaskItem(id=5, title="c", description="task c", files=["src/c.py"]),
    ]
    worker = PlanningMutatingWorker(
        plan_summary="Run tasks 3, 4, and 5 together.",
        plan_tasks=[
            {"task_id": 3, "files": ["src/a.py"]},
            {"task_id": 4, "files": ["src/b.py"]},
            {"task_id": 5, "files": ["src/c.py"]},
        ],
        task_writes={
            2: {
                "src/a.py": "after broad a\n",
                "src/b.py": "after broad b\n",
            },
            3: {"src/a.py": "after a\n"},
            4: {"src/b.py": "after b\n"},
            5: {"src/c.py": "after c\n"},
        },
    )
    flow._workers["do_work"] = worker  # type: ignore

    result = _run_do_work(flow, "plan output")

    assert worker.calls[0]["mode"] == "inspect"
    assert any(entry.kind == "batch_planning" for entry in flow.state.history)
    parallel_entries = [
        entry for entry in flow.state.task_executions if entry.parallel_batch_id == "b1"
    ]
    assert sorted(entry.task_id for entry in parallel_entries) == [3, 4, 5]
    assert all(entry.isolated_workspace for entry in parallel_entries)
    assert flow.state.tasks[1].files == ["src/a.py"]
    assert any(
        entry.task_id == 2 and entry.parallel_batch_id is None
        for entry in flow.state.task_executions
    )
    assert all(task.status == "done" for task in flow.state.tasks)
    assert sorted(flow.state.changed_files) == ["src/a.py", "src/b.py", "src/c.py"]
    assert "Task 3 complete" in result
    assert "Task 4 complete" in result
    assert "Task 5 complete" in result


def test_do_work_parallel_batch_planner_falls_back_to_static_selection_on_invalid_plan(
    tmp_path: Path,
):
    repo = _make_repo(
        tmp_path / "repo",
        {
            "src/a.py": "before a\n",
            "src/b.py": "before b\n",
        },
    )
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
        },
        workers={
            "do_work": {
                "worker": "claude",
                "model": "sonnet",
                "parallel": {
                    "enabled": True,
                    "max_workers": 2,
                    "planner": {"enabled": True},
                },
            }
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=[]),
        TaskItem(id=2, title="two", description="task two", files=["src/b.py"]),
    ]
    worker = PlanningMutatingWorker(
        plan_summary="Bad plan.",
        plan_tasks=[
            {"task_id": 999, "files": ["src/a.py"]},
        ],
        task_writes={
            1: {"src/a.py": "after a\n"},
            2: {"src/b.py": "after b\n"},
        },
    )
    flow._workers["do_work"] = worker  # type: ignore

    result = _run_do_work(flow, "plan output")

    assert len(worker.calls) == 3
    assert worker.calls[0]["mode"] == "inspect"
    assert worker.calls[1]["cwd"] == str(repo)
    assert worker.calls[2]["cwd"] == str(repo)
    assert [entry.kind for entry in flow.state.history] == [
        "task_complete",
        "task_complete",
    ]
    assert all(task.status == "done" for task in flow.state.tasks)
    assert "Task 1 complete" in result
    assert "Task 2 complete" in result


def test_do_work_parallel_batch_planner_same_size_result_does_not_override_static_batch_or_record_history(
    tmp_path: Path,
):
    repo = _make_repo(
        tmp_path / "repo",
        {
            "src/a.py": "before a\n",
            "src/b.py": "before b\n",
            "src/c.py": "before c\n",
        },
    )
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
        },
        workers={
            "do_work": {
                "worker": "claude",
                "model": "sonnet",
                "parallel": {
                    "enabled": True,
                    "max_workers": 3,
                    "planner": {"enabled": True},
                },
            }
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(
            id=2,
            title="broad",
            description="task broad",
            files=["src/a.py", "src/b.py"],
        ),
        TaskItem(id=3, title="a", description="task a", files=[]),
        TaskItem(id=4, title="b", description="task b", files=["src/b.py"]),
        TaskItem(id=5, title="c", description="task c", files=["src/c.py"]),
    ]
    worker = PlanningMutatingWorker(
        plan_summary="Run tasks 4 and 5 together.",
        plan_tasks=[
            {"task_id": 4, "files": ["src/b.py"]},
            {"task_id": 5, "files": ["src/c.py"]},
        ],
        task_writes={
            2: {
                "src/a.py": "after broad a\n",
                "src/b.py": "after broad b\n",
            },
            3: {"src/a.py": "after a\n"},
            4: {"src/b.py": "after b\n"},
            5: {"src/c.py": "after c\n"},
        },
    )
    flow._workers["do_work"] = worker  # type: ignore

    result = _run_do_work(flow, "plan output")

    assert worker.calls[0]["mode"] == "inspect"
    parallel_entries = [
        entry for entry in flow.state.task_executions if entry.parallel_batch_id == "b1"
    ]
    assert sorted(entry.task_id for entry in parallel_entries) == [2, 5]
    assert not any(entry.kind == "batch_planning" for entry in flow.state.history)
    assert flow.state.tasks[1].files == []
    assert flow.state.tasks[2].files == ["src/b.py"]
    assert all(task.status == "done" for task in flow.state.tasks)
    assert "Task 2 complete" in result
    assert "Task 5 complete" in result


def test_do_work_parallel_batch_planner_falls_back_to_static_selection_on_missing_file_hints(
    tmp_path: Path,
):
    repo = _make_repo(
        tmp_path / "repo",
        {
            "src/a.py": "before a\n",
            "src/b.py": "before b\n",
        },
    )
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
        },
        workers={
            "do_work": {
                "worker": "claude",
                "model": "sonnet",
                "parallel": {
                    "enabled": True,
                    "max_workers": 2,
                    "planner": {"enabled": True},
                },
            }
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=[]),
        TaskItem(id=2, title="two", description="task two", files=["src/b.py"]),
    ]
    worker = PlanningMutatingWorker(
        plan_summary="Run tasks 1 and 2 together.",
        plan_tasks=[
            {"task_id": 1, "files": []},
            {"task_id": 2, "files": ["src/b.py"]},
        ],
        task_writes={
            1: {"src/a.py": "after a\n"},
            2: {"src/b.py": "after b\n"},
        },
    )
    flow._workers["do_work"] = worker  # type: ignore

    result = _run_do_work(flow, "plan output")

    assert len(worker.calls) == 3
    assert worker.calls[0]["mode"] == "inspect"
    assert worker.calls[1]["cwd"] == str(repo)
    assert worker.calls[2]["cwd"] == str(repo)
    assert flow.state.tasks[0].files == []
    assert [entry.kind for entry in flow.state.history] == [
        "task_complete",
        "task_complete",
    ]
    assert all(task.status == "done" for task in flow.state.tasks)
    assert "Task 1 complete" in result
    assert "Task 2 complete" in result


def test_do_work_parallel_batch_planner_falls_back_to_static_selection_on_overlapping_file_hints(
    tmp_path: Path,
):
    repo = _make_repo(
        tmp_path / "repo",
        {
            "src/a.py": "before a\n",
            "src/b.py": "before b\n",
            "src/shared.py": "before shared\n",
        },
    )
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
        },
        workers={
            "do_work": {
                "worker": "claude",
                "model": "sonnet",
                "parallel": {
                    "enabled": True,
                    "max_workers": 2,
                    "planner": {"enabled": True},
                },
            }
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=[]),
        TaskItem(id=2, title="two", description="task two", files=[]),
    ]
    worker = PlanningMutatingWorker(
        plan_summary="Run tasks 1 and 2 together.",
        plan_tasks=[
            {"task_id": 1, "files": ["src/shared.py"]},
            {"task_id": 2, "files": ["src/shared.py"]},
        ],
        task_writes={
            1: {"src/a.py": "after a\n"},
            2: {"src/b.py": "after b\n"},
        },
    )
    flow._workers["do_work"] = worker  # type: ignore

    result = _run_do_work(flow, "plan output")

    assert len(worker.calls) == 3
    assert worker.calls[0]["mode"] == "inspect"
    assert worker.calls[1]["cwd"] == str(repo)
    assert worker.calls[2]["cwd"] == str(repo)
    assert flow.state.tasks[0].files == []
    assert flow.state.tasks[1].files == []
    assert [entry.kind for entry in flow.state.history] == [
        "task_complete",
        "task_complete",
    ]
    assert all(task.status == "done" for task in flow.state.tasks)
    assert "Task 1 complete" in result
    assert "Task 2 complete" in result


def test_do_work_parallel_path_rejects_overlapping_actual_changes(tmp_path: Path):
    repo = _make_repo(
        tmp_path / "repo",
        {
            "src/a.py": "before a\n",
            "src/b.py": "before b\n",
            "src/shared.py": "before shared\n",
        },
    )
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={
            "do_work": {
                "worker": "claude",
                "model": "sonnet",
                "parallel": {"enabled": True, "max_workers": 2},
            }
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"]),
        TaskItem(id=2, title="two", description="task two", files=["src/b.py"]),
    ]
    worker = MutatingWorker(
        {
            1: {"src/shared.py": "task one changed shared\n"},
            2: {"src/shared.py": "task two changed shared\n"},
        }
    )
    flow._workers["do_work"] = worker  # type: ignore

    result = _run_do_work(flow, "plan output")

    assert [task.status for task in flow.state.tasks] == ["failed", "failed"]
    assert flow.state.changed_files == []
    assert [entry.kind for entry in flow.state.history] == [
        "task_failed",
        "task_failed",
    ]
    assert all("src/shared.py" in error for error in flow.state.errors)
    assert (repo / "src/shared.py").read_text() == "before shared\n"
    assert "Parallel batch changed overlapping files: src/shared.py" in result


def test_do_work_crew_path_is_used_and_detects_changes(monkeypatch, tmp_path: Path):
    repo = _make_repo(tmp_path / "repo", {"src/a.py": "before\n"})
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={
            "do_work": {
                "worker": "claude",
                "model": "sonnet",
                "crew": {"enabled": True, "process": "sequential"},
            }
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"])
    ]
    worker = RecordingWorker()
    flow._workers["do_work"] = worker  # type: ignore

    calls: list[dict[str, Any]] = []

    def fake_run_do_work_crew(**kwargs):
        calls.append(kwargs)
        if observer := kwargs.get("round_observer"):
            observer(
                {
                    "round": 1,
                    "subtask_id": 1,
                    "subtask_title": "Wire flow",
                    "decision_status": "pass",
                    "decision_summary": "Crew verified the task.",
                    "decision_issues": [],
                    "result_summary": "implemented task",
                    "result_error": None,
                }
            )
        target = Path(kwargs["cwd"]) / "src/a.py"
        target.write_text("after\n")
        return (
            CoderResult(
                summary="implemented task",
                changed_files=[],
                raw_output="implemented task",
                exit_code=0,
            ),
            ReviewDecision(
                status="pass",
                issues=[],
                summary="Crew verified the task.",
            ),
        )

    monkeypatch.setattr(
        "crewai_headless_flow.stages.do_work.run_do_work_crew", fake_run_do_work_crew
    )

    result = _run_do_work(flow, "plan output")

    assert calls
    assert calls[0]["worker_tool"] is worker
    assert calls[0]["model"] == "sonnet"
    assert flow.state.tasks[0].status == "done"
    assert flow.state.changed_files == ["src/a.py"]
    assert flow.state.task_executions[0].orchestration == "crew"
    assert [
        round_entry.round for round_entry in flow.state.task_executions[0].crew_rounds
    ] == [1]
    assert flow.state.task_executions[0].crew_rounds[0].subtask_title == "Wire flow"
    assert (repo / "src/a.py").read_text() == "after\n"
    assert "Crew verified the task." in result


def test_do_work_crew_revise_marks_task_failed(monkeypatch, tmp_path: Path):
    repo = _make_repo(tmp_path / "repo", {"src/a.py": "before\n"})
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={
            "do_work": {
                "worker": "claude",
                "crew": {"enabled": True, "process": "sequential"},
            }
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"])
    ]
    flow._workers["do_work"] = RecordingWorker()  # type: ignore

    def fake_run_do_work_crew(**kwargs):
        if observer := kwargs.get("round_observer"):
            observer(
                {
                    "round": 1,
                    "decision_status": "revise",
                    "decision_summary": "Needs another pass.",
                    "decision_issues": ["Missing verification"],
                    "result_summary": "implemented task",
                    "result_error": None,
                }
            )
        return (
            CoderResult(
                summary="implemented task",
                changed_files=[],
                raw_output="implemented task",
                exit_code=0,
            ),
            ReviewDecision(
                status="revise",
                issues=["Missing verification"],
                summary="Needs another pass.",
            ),
        )

    monkeypatch.setattr(
        "crewai_headless_flow.stages.do_work.run_do_work_crew", fake_run_do_work_crew
    )

    result = _run_do_work(flow, "plan output")

    assert flow.state.tasks[0].status == "failed"
    assert (
        flow.state.tasks[0].last_error
        == "Implementation Crew requested revision: Missing verification"
    )
    assert flow.state.errors == [
        "Task 1 failed: Implementation Crew requested revision: Missing verification"
    ]
    assert "Task 1 failed" in result


def test_do_work_crew_stays_sequential_when_parallel_is_disabled(
    monkeypatch, tmp_path: Path
):
    repo = _make_repo(
        tmp_path / "repo",
        {"src/a.py": "before a\n", "src/b.py": "before b\n"},
    )
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={
            "do_work": {
                "worker": "claude",
                "crew": {"enabled": True, "process": "sequential"},
                "parallel": {"enabled": False, "max_workers": 2},
            }
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"]),
        TaskItem(id=2, title="two", description="task two", files=["src/b.py"]),
    ]
    flow._workers["do_work"] = RecordingWorker()  # type: ignore

    calls: list[str] = []

    def fake_run_do_work_crew(**kwargs):
        calls.append(kwargs["cwd"])
        if observer := kwargs.get("round_observer"):
            observer(
                {
                    "round": 1,
                    "decision_status": "pass",
                    "decision_summary": f"Crew verified task {_extract_task_id(kwargs['task_prompt'])}.",
                    "decision_issues": [],
                    "result_summary": "implemented task",
                    "result_error": None,
                }
            )
        task_id = _extract_task_id(kwargs["task_prompt"])
        target = Path(kwargs["cwd"]) / f"src/{'a' if task_id == 1 else 'b'}.py"
        target.write_text(f"after {task_id}\n")
        return (
            CoderResult(
                summary=f"implemented task {task_id}",
                changed_files=[],
                raw_output=f"implemented task {task_id}",
                exit_code=0,
            ),
            ReviewDecision(
                status="pass",
                issues=[],
                summary=f"Crew verified task {task_id}.",
            ),
        )

    monkeypatch.setattr(
        "crewai_headless_flow.stages.do_work.run_do_work_crew", fake_run_do_work_crew
    )

    result = _run_do_work(flow, "plan output")

    assert calls == [str(repo), str(repo)]
    assert all(task.status == "done" for task in flow.state.tasks)
    assert sorted(flow.state.changed_files) == ["src/a.py", "src/b.py"]
    assert "Task 1 complete" in result
    assert "Task 2 complete" in result


def test_do_work_parallel_crew_uses_isolated_workspaces_and_merges_disjoint_changes(
    monkeypatch, tmp_path: Path
):
    repo = _make_repo(
        tmp_path / "repo",
        {"src/a.py": "before a\n", "src/b.py": "before b\n"},
    )
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={
            "do_work": {
                "worker": "claude",
                "model": "sonnet",
                "crew": {"enabled": True, "process": "sequential"},
                "parallel": {"enabled": True, "max_workers": 2},
            }
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"]),
        TaskItem(id=2, title="two", description="task two", files=["src/b.py"]),
    ]
    flow._workers["do_work"] = RecordingWorker()  # type: ignore

    calls: list[str] = []

    def fake_run_do_work_crew(**kwargs):
        calls.append(kwargs["cwd"])
        task_id = _extract_task_id(kwargs["task_prompt"])
        target = Path(kwargs["cwd"]) / f"src/{'a' if task_id == 1 else 'b'}.py"
        target.write_text(f"after {task_id}\n")
        return (
            CoderResult(
                summary=f"implemented task {task_id}",
                changed_files=[],
                raw_output=f"implemented task {task_id}",
                exit_code=0,
            ),
            ReviewDecision(
                status="pass",
                issues=[],
                summary=f"Crew verified task {task_id}.",
            ),
        )

    monkeypatch.setattr(
        "crewai_headless_flow.stages.do_work.run_do_work_crew", fake_run_do_work_crew
    )

    result = _run_do_work(flow, "plan output")

    assert len(calls) == 2
    assert all(call != str(repo) for call in calls)
    assert len(set(calls)) == 2
    assert all(task.status == "done" for task in flow.state.tasks)
    assert sorted(flow.state.changed_files) == ["src/a.py", "src/b.py"]
    assert (repo / "src/a.py").read_text() == "after 1\n"
    assert (repo / "src/b.py").read_text() == "after 2\n"
    assert "Task 1 complete" in result
    assert "Task 2 complete" in result


def test_do_work_parallel_crew_rejects_overlapping_actual_changes(
    monkeypatch, tmp_path: Path
):
    repo = _make_repo(
        tmp_path / "repo",
        {
            "src/a.py": "before a\n",
            "src/b.py": "before b\n",
            "src/shared.py": "before shared\n",
        },
    )
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={
            "do_work": {
                "worker": "claude",
                "crew": {"enabled": True, "process": "sequential"},
                "parallel": {"enabled": True, "max_workers": 2},
            }
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"]),
        TaskItem(id=2, title="two", description="task two", files=["src/b.py"]),
    ]
    flow._workers["do_work"] = RecordingWorker()  # type: ignore

    def fake_run_do_work_crew(**kwargs):
        if observer := kwargs.get("round_observer"):
            observer(
                {
                    "round": 1,
                    "decision_status": "pass",
                    "decision_summary": "Crew verified task.",
                    "decision_issues": [],
                    "result_summary": "implemented task",
                    "result_error": None,
                }
            )
        target = Path(kwargs["cwd"]) / "src/shared.py"
        target.write_text("changed shared\n")
        return (
            CoderResult(
                summary="implemented task",
                changed_files=[],
                raw_output="implemented task",
                exit_code=0,
            ),
            ReviewDecision(
                status="pass",
                issues=[],
                summary="Crew verified task.",
            ),
        )

    monkeypatch.setattr(
        "crewai_headless_flow.stages.do_work.run_do_work_crew", fake_run_do_work_crew
    )

    result = _run_do_work(flow, "plan output")

    assert [task.status for task in flow.state.tasks] == ["failed", "failed"]
    assert flow.state.changed_files == []
    assert all("src/shared.py" in error for error in flow.state.errors)
    assert (repo / "src/shared.py").read_text() == "before shared\n"
    assert "Parallel batch changed overlapping files: src/shared.py" in result


def test_structured_revise_targets_hint_tasks_only_and_reruns_them():
    flow = CrewAIHeadlessFlow()
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(
            id=1,
            title="one",
            description="task one",
            files=["src/a.py"],
            status="done",
        ),
        TaskItem(
            id=2,
            title="two",
            description="task two",
            files=["src/b.py"],
            status="done",
        ),
    ]
    flow.state.issues = ["Fix task one"]
    flow.state.review_task_hints = [
        {"task_ids": [1], "files": ["src/a.py"], "summary": "Task 1 needs revision"}
    ]

    prompt = flow.revise("revise")

    assert flow.state.tasks[0].status == "needs_revision"
    assert flow.state.tasks[0].review_notes == ["Task 1 needs revision"]
    assert flow.state.tasks[1].status == "done"
    assert flow.state.history[-1].kind == "revision_targeting"
    assert "Task 1" in prompt
    assert "Recent history:" in prompt

    worker = RecordingWorker()
    flow._workers["do_work"] = worker  # type: ignore

    result = flow.do_work(prompt)

    assert len(worker.calls) == 1
    assert flow.state.tasks[0].status == "done"
    assert flow.state.tasks[1].status == "done"
    assert "Task 1 complete" in result


def test_structured_revise_fallback_reopens_all_completed_tasks_when_unmapped():
    flow = CrewAIHeadlessFlow()
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(
            id=1,
            title="one",
            description="task one",
            files=["src/a.py"],
            status="done",
        ),
        TaskItem(
            id=2,
            title="two",
            description="task two",
            files=["src/b.py"],
            status="done",
        ),
    ]
    flow.state.issues = ["General regression remains"]

    flow.revise("revise")

    assert flow.state.tasks[0].status == "needs_revision"
    assert flow.state.tasks[1].status == "needs_revision"


def test_structured_revise_reopens_downstream_dependents_conservatively():
    flow = CrewAIHeadlessFlow()
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(
            id=1,
            title="one",
            description="task one",
            files=["src/a.py"],
            status="done",
        ),
        TaskItem(
            id=2,
            title="two",
            description="task two",
            files=["src/b.py"],
            dependencies=[1],
            status="done",
        ),
    ]
    flow.state.issues = ["Fix upstream task"]
    flow.state.review_task_hints = [
        {"task_ids": [1], "files": ["src/a.py"], "summary": "Task 1 needs revision"}
    ]

    flow.revise("revise")

    assert flow.state.tasks[0].status == "needs_revision"
    assert flow.state.tasks[1].status == "needs_revision"
    assert flow.state.tasks[1].review_notes == [
        "Upstream dependency requires revision."
    ]


def test_structured_revise_replanner_replaces_task_graph_and_preserves_unaffected_done_tasks():
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
        },
        workers={
            "plan": {"worker": "codex"},
            "do_work": {"worker": "codex", "replan": {"enabled": True}},
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow.state.spec = "Original plan."
    flow.state.tasks = [
        TaskItem(
            id=1,
            title="one",
            description="task one",
            files=["src/a.py"],
            status="done",
        ),
        TaskItem(
            id=2,
            title="two",
            description="task two",
            files=["src/b.py"],
            dependencies=[1],
            status="done",
        ),
        TaskItem(
            id=3,
            title="docs",
            description="docs task",
            files=["docs/readme.md"],
            status="done",
        ),
    ]
    flow.state.issues = ["Task one needs to be split into code plus test work"]
    flow.state.review_task_hints = [
        {"task_ids": [1], "files": ["src/a.py"], "summary": "Task 1 needs revision"}
    ]
    flow._workers["plan"] = RecordingWorker(
        inspect_raw=json.dumps(
            {
                "spec": "Replanned spec.",
                "tasks": [
                    {
                        "id": 11,
                        "title": "fix code",
                        "description": "repair src/a.py",
                        "acceptance_criteria": ["code fixed"],
                        "verification": ["pytest -q"],
                        "dependencies": [],
                        "files": ["src/a.py"],
                        "estimated_scope": "S",
                    },
                    {
                        "id": 12,
                        "title": "add test",
                        "description": "cover regression",
                        "acceptance_criteria": ["test added"],
                        "verification": ["pytest -q"],
                        "dependencies": [11],
                        "files": ["tests/test_a.py"],
                        "estimated_scope": "S",
                    },
                    {
                        "id": 2,
                        "title": "two",
                        "description": "task two",
                        "acceptance_criteria": [],
                        "verification": [],
                        "dependencies": [12],
                        "files": ["src/b.py"],
                        "estimated_scope": None,
                    },
                    {
                        "id": 3,
                        "title": "docs",
                        "description": "docs task",
                        "acceptance_criteria": [],
                        "verification": [],
                        "dependencies": [],
                        "files": ["docs/readme.md"],
                        "estimated_scope": None,
                    },
                ],
            }
        )
    )  # type: ignore

    prompt = flow.revise("revise")

    assert flow.state.spec == "Replanned spec."
    assert [task.id for task in flow.state.tasks] == [11, 12, 2, 3]
    assert [task.status for task in flow.state.tasks] == [
        "pending",
        "pending",
        "pending",
        "done",
    ]
    assert flow.state.review_task_hints == []
    assert flow.state.history[-1].kind == "revision_replanning"
    assert "Task 11" in prompt
    assert "Task 12" in prompt
    assert "Task 3" not in prompt


def test_structured_revise_replanner_falls_back_to_targeted_revision_when_invalid():
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
        },
        workers={
            "plan": {"worker": "codex"},
            "do_work": {"worker": "codex", "replan": {"enabled": True}},
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(
            id=1,
            title="one",
            description="task one",
            files=["src/a.py"],
            status="done",
        ),
        TaskItem(
            id=2,
            title="two",
            description="task two",
            files=["src/b.py"],
            status="done",
        ),
    ]
    flow.state.issues = ["Fix task one"]
    flow.state.review_task_hints = [
        {"task_ids": [1], "files": ["src/a.py"], "summary": "Task 1 needs revision"}
    ]
    flow._workers["plan"] = RecordingWorker(inspect_raw='{"spec": "", "tasks": []}')  # type: ignore

    prompt = flow.revise("revise")

    assert flow.state.tasks[0].status == "needs_revision"
    assert flow.state.tasks[1].status == "done"
    assert flow.state.history[-1].kind == "revision_targeting"
    assert "Task 1" in prompt


def test_do_work_execution_replanner_recovers_from_sequential_task_failure(
    tmp_path: Path,
):
    repo = _make_repo(
        tmp_path / "repo",
        {
            "src/a.py": "before a\n",
            "docs/readme.md": "docs\n",
        },
    )
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
        },
        workers={
            "plan": {"worker": "codex"},
            "do_work": {
                "worker": "codex",
                "replan": {
                    "enabled": True,
                    "on_execution_failure": True,
                    "max_execution_replans": 1,
                },
            },
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.spec = "Original plan."
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"]),
        TaskItem(
            id=9,
            title="docs",
            description="docs task",
            files=["docs/readme.md"],
            status="done",
        ),
    ]
    flow._workers["plan"] = RecordingWorker(
        inspect_raw=json.dumps(
            {
                "spec": "Runtime recovery plan.",
                "tasks": [
                    {
                        "id": 11,
                        "title": "repair code",
                        "description": "finish src/a.py",
                        "acceptance_criteria": ["code repaired"],
                        "verification": ["pytest -q"],
                        "dependencies": [],
                        "files": ["src/a.py"],
                        "estimated_scope": "S",
                    },
                    {
                        "id": 12,
                        "title": "add regression test",
                        "description": "cover recovered bug",
                        "acceptance_criteria": ["test added"],
                        "verification": ["pytest -q"],
                        "dependencies": [11],
                        "files": ["tests/test_a.py"],
                        "estimated_scope": "S",
                    },
                    {
                        "id": 9,
                        "title": "docs",
                        "description": "docs task",
                        "acceptance_criteria": [],
                        "verification": [],
                        "dependencies": [],
                        "files": ["docs/readme.md"],
                        "estimated_scope": None,
                    },
                ],
            }
        )
    )  # type: ignore
    flow._workers["do_work"] = FailingMutatingWorker(
        failing_tasks={1: ({"src/a.py": "partial failure\n"}, "compile failed")},
        success_writes={
            11: {"src/a.py": "recovered code\n"},
            12: {"tests/test_a.py": "test recovered\n"},
        },
    )  # type: ignore

    result = _run_do_work(flow, "plan output")

    assert flow.state.spec == "Runtime recovery plan."
    assert [task.id for task in flow.state.tasks] == [11, 12, 9]
    assert all(task.status == "done" for task in flow.state.tasks)
    assert sorted(flow.state.changed_files) == ["src/a.py", "tests/test_a.py"]
    assert [entry.kind for entry in flow.state.history] == [
        "execution_replanning",
        "task_complete",
        "task_complete",
    ]
    assert (repo / "src/a.py").read_text() == "recovered code\n"
    assert (repo / "tests/test_a.py").read_text() == "test recovered\n"
    assert "replanned remaining work" in result
    assert "Task 11 complete" in result
    assert "Task 12 complete" in result


def test_do_work_execution_replanner_recovers_from_parallel_batch_failure(
    tmp_path: Path,
):
    repo = _make_repo(
        tmp_path / "repo",
        {
            "src/a.py": "before a\n",
            "src/b.py": "before b\n",
        },
    )
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
        },
        workers={
            "plan": {"worker": "codex"},
            "do_work": {
                "worker": "codex",
                "parallel": {"enabled": True, "max_workers": 2},
                "replan": {
                    "enabled": True,
                    "on_execution_failure": True,
                    "max_execution_replans": 1,
                },
            },
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.spec = "Original parallel plan."
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"]),
        TaskItem(id=2, title="two", description="task two", files=["src/b.py"]),
    ]
    flow._workers["plan"] = RecordingWorker(
        inspect_raw=json.dumps(
            {
                "spec": "Parallel recovery plan.",
                "tasks": [
                    {
                        "id": 11,
                        "title": "repair failed branch",
                        "description": "recover src/a.py",
                        "acceptance_criteria": ["branch recovered"],
                        "verification": ["pytest -q"],
                        "dependencies": [],
                        "files": ["src/a.py"],
                        "estimated_scope": "S",
                    },
                    {
                        "id": 2,
                        "title": "two",
                        "description": "task two",
                        "acceptance_criteria": [],
                        "verification": [],
                        "dependencies": [],
                        "files": ["src/b.py"],
                        "estimated_scope": None,
                    },
                ],
            }
        )
    )  # type: ignore
    flow._workers["do_work"] = FailingMutatingWorker(
        failing_tasks={
            1: ({"src/a.py": "partial parallel failure\n"}, "branch failed")
        },
        success_writes={
            2: {"src/b.py": "parallel success\n"},
            11: {"src/a.py": "parallel recovered\n"},
        },
    )  # type: ignore

    result = _run_do_work(flow, "plan output")

    assert flow.state.spec == "Parallel recovery plan."
    assert [task.id for task in flow.state.tasks] == [11, 2]
    assert all(task.status == "done" for task in flow.state.tasks)
    assert sorted(flow.state.changed_files) == ["src/a.py", "src/b.py"]
    assert [entry.kind for entry in flow.state.history] == [
        "task_complete",
        "execution_replanning",
        "task_complete",
    ]
    assert (repo / "src/a.py").read_text() == "parallel recovered\n"
    assert (repo / "src/b.py").read_text() == "parallel success\n"
    assert "replanned remaining work" in result
    assert "Task 2 complete" in result
    assert "Task 11 complete" in result


def test_do_work_cross_task_success_replanner_recovers_from_sequential_overlap(
    tmp_path: Path,
):
    repo = _make_repo(
        tmp_path / "repo",
        {
            "src/a.py": "before a\n",
            "src/b.py": "before b\n",
            "docs/readme.md": "docs\n",
        },
    )
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
        },
        workers={
            "plan": {"worker": "codex"},
            "do_work": {
                "worker": "codex",
                "replan": {
                    "enabled": True,
                    "on_cross_task_change": True,
                    "max_execution_replans": 1,
                },
            },
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.spec = "Original sequential plan."
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"]),
        TaskItem(id=2, title="two", description="task two", files=["src/b.py"]),
        TaskItem(
            id=9,
            title="docs",
            description="docs task",
            files=["docs/readme.md"],
            status="done",
        ),
    ]
    plan_worker = RecordingWorker(
        inspect_raw=json.dumps(
            {
                "spec": "Cross-task sequential recovery plan.",
                "tasks": [
                    {
                        "id": 1,
                        "title": "one",
                        "description": "task one",
                        "acceptance_criteria": [],
                        "verification": [],
                        "dependencies": [],
                        "files": ["src/a.py"],
                        "estimated_scope": None,
                    },
                    {
                        "id": 21,
                        "title": "finish shared file",
                        "description": "finalize src/b.py after upstream spillover",
                        "acceptance_criteria": ["shared file finalized"],
                        "verification": ["pytest -q"],
                        "dependencies": [1],
                        "files": ["src/b.py", "tests/test_b.py"],
                        "estimated_scope": "S",
                    },
                    {
                        "id": 9,
                        "title": "docs",
                        "description": "docs task",
                        "acceptance_criteria": [],
                        "verification": [],
                        "dependencies": [],
                        "files": ["docs/readme.md"],
                        "estimated_scope": None,
                    },
                ],
            }
        )
    )
    flow._workers["plan"] = plan_worker  # type: ignore[assignment]
    flow._workers["do_work"] = MutatingWorker(
        task_writes={
            1: {
                "src/a.py": "done a\n",
                "src/b.py": "shared overlap from task 1\n",
            },
            21: {
                "src/b.py": "replanned b\n",
                "tests/test_b.py": "regression\n",
            },
        }
    )  # type: ignore

    result = _run_do_work(flow, "plan output")

    assert flow.state.spec == "Cross-task sequential recovery plan."
    assert [task.id for task in flow.state.tasks] == [1, 21, 9]
    assert all(task.status == "done" for task in flow.state.tasks)
    assert sorted(flow.state.changed_files) == [
        "src/a.py",
        "src/b.py",
        "tests/test_b.py",
    ]
    assert [entry.kind for entry in flow.state.history] == [
        "task_complete",
        "execution_replanning",
        "task_complete",
    ]
    assert (
        "changed files assigned to other remaining tasks"
        in plan_worker.calls[0]["task"]
    )
    assert (repo / "src/a.py").read_text() == "done a\n"
    assert (repo / "src/b.py").read_text() == "replanned b\n"
    assert (repo / "tests/test_b.py").read_text() == "regression\n"
    assert "replanned remaining work from cross-task changes" in result
    assert "Task 21 complete" in result


def test_do_work_cross_task_success_replanner_recovers_from_parallel_success_overlap(
    tmp_path: Path,
):
    repo = _make_repo(
        tmp_path / "repo",
        {
            "src/a.py": "before a\n",
            "src/b.py": "before b\n",
            "src/c.py": "before c\n",
        },
    )
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
        },
        workers={
            "plan": {"worker": "codex"},
            "do_work": {
                "worker": "codex",
                "parallel": {"enabled": True, "max_workers": 2},
                "replan": {
                    "enabled": True,
                    "on_cross_task_change": True,
                    "max_execution_replans": 1,
                },
            },
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.spec = "Original parallel plan."
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"]),
        TaskItem(id=2, title="two", description="task two", files=["src/b.py"]),
        TaskItem(
            id=3,
            title="three",
            description="task three",
            files=["src/c.py"],
            dependencies=[1, 2],
        ),
    ]
    plan_worker = RecordingWorker(
        inspect_raw=json.dumps(
            {
                "spec": "Cross-task parallel recovery plan.",
                "tasks": [
                    {
                        "id": 1,
                        "title": "one",
                        "description": "task one",
                        "acceptance_criteria": [],
                        "verification": [],
                        "dependencies": [],
                        "files": ["src/a.py"],
                        "estimated_scope": None,
                    },
                    {
                        "id": 2,
                        "title": "two",
                        "description": "task two",
                        "acceptance_criteria": [],
                        "verification": [],
                        "dependencies": [],
                        "files": ["src/b.py"],
                        "estimated_scope": None,
                    },
                    {
                        "id": 31,
                        "title": "finish shared downstream file",
                        "description": "finalize src/c.py after upstream spillover",
                        "acceptance_criteria": ["shared downstream file finalized"],
                        "verification": ["pytest -q"],
                        "dependencies": [1, 2],
                        "files": ["src/c.py", "tests/test_c.py"],
                        "estimated_scope": "S",
                    },
                ],
            }
        )
    )
    flow._workers["plan"] = plan_worker  # type: ignore[assignment]
    flow._workers["do_work"] = MutatingWorker(
        task_writes={
            1: {
                "src/a.py": "parallel a\n",
                "src/c.py": "spillover from task 1\n",
            },
            2: {"src/b.py": "parallel b\n"},
            31: {
                "src/c.py": "replanned c\n",
                "tests/test_c.py": "parallel regression\n",
            },
        }
    )  # type: ignore

    result = _run_do_work(flow, "plan output")

    assert flow.state.spec == "Cross-task parallel recovery plan."
    assert [task.id for task in flow.state.tasks] == [1, 2, 31]
    assert all(task.status == "done" for task in flow.state.tasks)
    assert sorted(flow.state.changed_files) == [
        "src/a.py",
        "src/b.py",
        "src/c.py",
        "tests/test_c.py",
    ]
    assert [entry.kind for entry in flow.state.history] == [
        "task_complete",
        "task_complete",
        "execution_replanning",
        "task_complete",
    ]
    assert sorted(entry.task_id for entry in flow.state.task_executions) == [1, 2, 31]
    assert (
        "changed files assigned to other remaining tasks"
        in plan_worker.calls[0]["task"]
    )
    assert (repo / "src/a.py").read_text() == "parallel a\n"
    assert (repo / "src/b.py").read_text() == "parallel b\n"
    assert (repo / "src/c.py").read_text() == "replanned c\n"
    assert (repo / "tests/test_c.py").read_text() == "parallel regression\n"
    assert "replanned remaining work from cross-task changes" in result
    assert "Task 31 complete" in result


def test_do_work_ambiguous_success_replanner_recovers_from_sequential_noop_success(
    tmp_path: Path,
):
    repo = _make_repo(
        tmp_path / "repo",
        {
            "src/a.py": "before a\n",
            "docs/readme.md": "docs\n",
        },
    )
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
        },
        workers={
            "plan": {"worker": "codex"},
            "do_work": {
                "worker": "codex",
                "replan": {
                    "enabled": True,
                    "on_ambiguous_success": True,
                    "max_execution_replans": 1,
                },
            },
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.spec = "Original noop plan."
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"]),
        TaskItem(
            id=9,
            title="docs",
            description="docs task",
            files=["docs/readme.md"],
            status="done",
        ),
    ]
    plan_worker = RecordingWorker(
        inspect_raw=json.dumps(
            {
                "spec": "Ambiguous-success recovery plan.",
                "tasks": [
                    {
                        "id": 11,
                        "title": "actually implement task one",
                        "description": "finish src/a.py after noop success",
                        "acceptance_criteria": ["src/a.py updated"],
                        "verification": ["pytest -q"],
                        "dependencies": [],
                        "files": ["src/a.py"],
                        "estimated_scope": "S",
                    },
                    {
                        "id": 9,
                        "title": "docs",
                        "description": "docs task",
                        "acceptance_criteria": [],
                        "verification": [],
                        "dependencies": [],
                        "files": ["docs/readme.md"],
                        "estimated_scope": None,
                    },
                ],
            }
        )
    )
    flow._workers["plan"] = plan_worker  # type: ignore[assignment]
    flow._workers["do_work"] = MutatingWorker(
        task_writes={
            1: {},
            11: {"src/a.py": "implemented after noop\n"},
        }
    )  # type: ignore

    result = _run_do_work(flow, "plan output")

    assert flow.state.spec == "Ambiguous-success recovery plan."
    assert [task.id for task in flow.state.tasks] == [11, 9]
    assert all(task.status == "done" for task in flow.state.tasks)
    assert flow.state.changed_files == ["src/a.py"]
    assert [entry.kind for entry in flow.state.history] == [
        "task_complete",
        "execution_replanning",
        "task_complete",
    ]
    assert "no files changed" in plan_worker.calls[0]["task"]
    assert (repo / "src/a.py").read_text() == "implemented after noop\n"
    assert "replanned remaining work from ambiguous success evidence" in result
    assert "Task 11 complete" in result


def test_do_work_ambiguous_success_replanner_recovers_from_parallel_off_target_success(
    tmp_path: Path,
):
    repo = _make_repo(
        tmp_path / "repo",
        {
            "src/a.py": "before a\n",
            "src/b.py": "before b\n",
        },
    )
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
        },
        workers={
            "plan": {"worker": "codex"},
            "do_work": {
                "worker": "codex",
                "parallel": {"enabled": True, "max_workers": 2},
                "replan": {
                    "enabled": True,
                    "on_ambiguous_success": True,
                    "max_execution_replans": 1,
                },
            },
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.spec = "Original off-target plan."
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"]),
        TaskItem(id=2, title="two", description="task two", files=["src/b.py"]),
    ]
    plan_worker = RecordingWorker(
        inspect_raw=json.dumps(
            {
                "spec": "Off-target recovery plan.",
                "tasks": [
                    {
                        "id": 41,
                        "title": "repair task one",
                        "description": "finish src/a.py after off-target success",
                        "acceptance_criteria": ["src/a.py updated"],
                        "verification": ["pytest -q"],
                        "dependencies": [],
                        "files": ["src/a.py"],
                        "estimated_scope": "S",
                    },
                    {
                        "id": 2,
                        "title": "two",
                        "description": "task two",
                        "acceptance_criteria": [],
                        "verification": [],
                        "dependencies": [],
                        "files": ["src/b.py"],
                        "estimated_scope": None,
                    },
                ],
            }
        )
    )
    flow._workers["plan"] = plan_worker  # type: ignore[assignment]
    flow._workers["do_work"] = MutatingWorker(
        task_writes={
            1: {"src/generated.py": "off target output\n"},
            2: {"src/b.py": "parallel b\n"},
            41: {"src/a.py": "fixed after off target\n"},
        }
    )  # type: ignore

    result = _run_do_work(flow, "plan output")

    assert flow.state.spec == "Off-target recovery plan."
    assert [task.id for task in flow.state.tasks] == [41, 2]
    assert all(task.status == "done" for task in flow.state.tasks)
    assert sorted(flow.state.changed_files) == [
        "src/a.py",
        "src/b.py",
        "src/generated.py",
    ]
    assert [entry.kind for entry in flow.state.history] == [
        "task_complete",
        "task_complete",
        "execution_replanning",
        "task_complete",
    ]
    assert "none of its planned files were changed" in plan_worker.calls[0]["task"]
    assert (repo / "src/a.py").read_text() == "fixed after off target\n"
    assert (repo / "src/b.py").read_text() == "parallel b\n"
    assert (repo / "src/generated.py").read_text() == "off target output\n"
    assert "replanned remaining work from ambiguous success evidence" in result
    assert "Task 41 complete" in result


def test_review_infers_task_hint_from_issue_text_when_model_omits_hints():
    flow = CrewAIHeadlessFlow()
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"]),
        TaskItem(id=2, title="two", description="task two", files=["src/b.py"]),
    ]
    flow._workers["review"] = StubWorker(
        raw_review_output='{"status": "revise", "issues": ["Task 2 needs another test"], "summary": "Needs work"}'
    )  # type: ignore

    decision = flow.review("work summary")

    assert decision == "revise"
    assert len(flow.state.review_task_hints) == 1
    assert flow.state.review_task_hints[0].task_ids == [2]
    assert flow.state.history[-1].kind == "review_decision"


def test_review_infers_task_hint_from_file_path_when_model_omits_hints():
    flow = CrewAIHeadlessFlow()
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"]),
        TaskItem(id=2, title="two", description="task two", files=["src/b.py"]),
    ]
    flow._workers["review"] = StubWorker(
        raw_review_output='{"status": "revise", "issues": ["src/a.py still has edge-case bug"], "summary": "Needs work"}'
    )  # type: ignore

    decision = flow.review("work summary")

    assert decision == "revise"
    assert len(flow.state.review_task_hints) == 1
    assert flow.state.review_task_hints[0].task_ids == [1]
    assert flow.state.review_task_hints[0].files == ["src/a.py"]


def test_review_passes_configured_model_to_worker():
    cfg = FlowConfig(
        skills={"review": "code-review-and-quality"},
        workers={"review": {"worker": "claude", "model": "sonnet"}},
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    worker = RecordingWorker()
    flow._workers["review"] = worker  # type: ignore

    flow.review("work summary")

    assert worker.calls[0]["model"] == "sonnet"
    assert worker.calls[0]["schema"] == REVIEW_DECISION_SCHEMA


def test_finalize_passes_configured_model_to_worker():
    cfg = FlowConfig(
        skills={"finalize": "documentation-and-adrs"},
        workers={"finalize": {"worker": "claude", "model": "sonnet"}},
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    worker = RecordingWorker()
    flow._workers["finalize"] = worker  # type: ignore
    flow.state.history = [
        {"kind": "review_decision", "revision": 1, "summary": "Review returned pass."}
    ]

    flow.finalize("pass")

    assert worker.calls[0]["model"] == "sonnet"
    assert "Recent flow history:" in worker.calls[0]["task"]
    assert "Review returned pass." in worker.calls[0]["task"]
    assert flow.state.debug_report is not None


def test_router_returns_pass_on_good_review():
    flow = CrewAIHeadlessFlow()

    # Manually set state for isolated method testing (avoids triggering real LLM in @start)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    # Inject stub for review stage
    stub = StubWorker(review_outcome="pass")
    flow._workers["review"] = stub  # type: ignore

    decision = flow.review("some work summary")

    assert decision == "pass"
    assert flow.state.review_status == "pass"
    assert stub.call_count == 1


def test_router_fails_closed_when_structured_tasks_are_incomplete():
    flow = CrewAIHeadlessFlow()
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        tasks=[
            TaskItem(
                id=1,
                title="api",
                description="update api",
                files=["src/api.py"],
                status="done",
            ),
            TaskItem(
                id=2,
                title="docs",
                description="update docs",
                files=["docs/readme.md"],
                status="pending",
            ),
        ],
    )
    flow._workers["review"] = StubWorker(review_outcome="pass")  # type: ignore

    decision = flow.review("some work summary")

    assert decision == "revise"
    assert flow.state.review_status == "revise"
    assert flow.state.issues == ["Structured tasks remain incomplete: 2"]
    assert len(flow.state.review_task_hints) == 1
    assert flow.state.review_task_hints[0].task_ids == [2]
    assert flow.state.review_task_hints[0].files == ["docs/readme.md"]
    assert (
        flow.state.review_task_hints[0].summary
        == "Complete the remaining structured tasks before review can pass."
    )


def test_router_returns_revise_and_loop_works():
    flow = CrewAIHeadlessFlow()
    flow._state = FlowState(request="test", target_repo="/tmp/fake", max_revisions=2)  # type: ignore[attr-defined]

    # First review fails
    stub = StubWorker(review_outcome="revise", issues=["Missing tests"])
    flow._workers["review"] = stub  # type: ignore

    decision = flow.review("work summary")
    assert decision == "revise"
    assert flow.state.review_status == "revise"

    # Simulate the revise listener
    flow.revise(decision)
    assert flow.state.revisions == 1

    # Second review now passes
    stub2 = StubWorker(review_outcome="pass")
    flow._workers["review"] = stub2  # type: ignore

    decision2 = flow.review("fixed work")
    assert decision2 == "pass"


def test_router_normalizes_invalid_review_status_to_revise():
    flow = CrewAIHeadlessFlow()
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow._workers["review"] = StubWorker(
        raw_review_output='{"status": "approve", "issues": [], "summary": "Bad status"}'
    )  # type: ignore

    decision = flow.review("work summary")

    assert decision == "revise"
    assert flow.state.review_status == "revise"
    assert flow.state.issues == ["Review returned invalid status: approve"]


def test_router_coerces_non_list_review_issues_to_list():
    flow = CrewAIHeadlessFlow()
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow._workers["review"] = StubWorker(
        raw_review_output='{"status": "revise", "issues": "Missing tests", "summary": "Needs work"}'
    )  # type: ignore

    decision = flow.review("work summary")

    assert decision == "revise"
    assert flow.state.issues == ["Missing tests"]


def test_router_parses_claude_json_wrapper_review_payload():
    flow = CrewAIHeadlessFlow()
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    inner = '{"status": "pass", "issues": [], "summary": "Looks good"}'

    class ClaudeWrappedReviewWorker:
        call_count = 0

        def run(self, task: str, cwd: str, mode: str = "edit", **kwargs):
            self.call_count += 1
            return CoderResult(
                summary=inner,
                raw_output='{"type": "result", "result": ' + json.dumps(inner) + "}",
                exit_code=0,
            )

    stub = ClaudeWrappedReviewWorker()
    flow._workers["review"] = stub  # type: ignore

    decision = flow.review("work summary")

    assert decision == "pass"
    assert flow.state.review_status == "pass"
    assert flow.state.issues == []
    assert stub.call_count == 1


def test_max_revisions_caps_the_loop():
    flow = CrewAIHeadlessFlow()
    flow._state = FlowState(request="test", target_repo="/tmp/fake", max_revisions=1)  # type: ignore[attr-defined]

    stub = StubWorker(
        raw_review_output='{"status": "revise", "issues": ["bad"], "summary": "Needs work"}'
    )
    flow._workers["review"] = stub  # type: ignore

    decision = flow.review("work")
    result = flow.revise(decision)

    assert decision == "revise"
    assert result == "failed"
    assert flow.state.revisions == 1
    assert flow.state.revisions <= flow.state.max_revisions
    assert flow.state.status == "failed"
    assert flow.state.review_status == "revise"
    assert flow.state.last_stage == "revise"
    assert flow.state.issues == [
        "bad",
        "Max revisions reached before review could pass.",
    ]
    assert flow.state.errors[-1] == "Max revisions reached before review could pass."
    assert flow.state.history[-1].kind == "review_decision"
    assert flow.state.history[-1].summary == "Flow failed after max revisions."


def test_human_abort_routes_to_terminal_without_review_worker():
    flow = CrewAIHeadlessFlow()
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        status="aborted_by_human",
        aborted_stage="do_work",
    )
    stub = StubWorker(review_outcome="pass")
    flow._workers["review"] = stub  # type: ignore

    decision = flow.review("aborted-by-human")

    assert decision == "aborted"
    assert stub.call_count == 0
    assert flow.state.status == "aborted_by_human"
    assert flow.state.aborted_stage == "do_work"


def test_flow_state_upgrades_legacy_aborted_fields_to_structured_checkpoint():
    state = FlowState.model_validate(
        {
            "status": "aborted_by_human",
            "aborted_stage": "review",
            "aborted_gate": "after_review",
            "aborted_gate_message": "Automated review completed.",
            "aborted_before_review_instructions": "Focus on migrations",
            "aborted_stage_input": "work summary",
        }
    )

    assert state.aborted_checkpoint == AbortedCheckpoint(
        stage="review",
        gate="after_review",
        message="Automated review completed.",
        before_review_instructions="Focus on migrations",
        stage_input="work summary",
    )

    dumped = state.model_dump()

    assert dumped["aborted_checkpoint"] == {
        "stage": "review",
        "gate": "after_review",
        "message": "Automated review completed.",
        "before_review_instructions": "Focus on migrations",
        "stage_input": "work summary",
    }
    assert dumped["aborted_stage"] == "review"


def test_human_abort_does_not_increment_revise_loop():
    flow = CrewAIHeadlessFlow()
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        status="aborted_by_human",
        aborted_stage="do_work",
    )

    result = flow.revise("revise")

    assert result == "aborted-by-human"
    assert flow.state.revisions == 0
    assert flow.state.status == "aborted_by_human"


def test_human_abort_does_not_run_finalize_worker():
    flow = CrewAIHeadlessFlow()
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        status="aborted_by_human",
        aborted_stage="do_work",
    )
    stub = StubWorker()
    flow._workers["finalize"] = stub  # type: ignore

    result = flow.finalize("pass")

    assert result == "aborted-by-human"
    assert stub.call_count == 0
    assert flow.state.status == "aborted_by_human"


def test_resume_headless_flow_restarts_from_aborted_do_work(monkeypatch):
    calls: list[tuple[str, str]] = []

    class FakeFlow:
        def __init__(self, config=None, run_store=None):
            self.config = config
            self._run_store = run_store
            self._state = None

        @property
        def state(self):
            return self._state

        def _refresh_debug_report(self):
            return None

        def do_work(self, plan_output: str) -> str:
            assert self.state.status == "running"
            assert self.state.aborted_stage is None
            calls.append(("do_work", plan_output))
            self.state.status = "running"
            return "work summary"

        def review(self, work_summary: str) -> str:
            calls.append(("review", work_summary))
            return "pass"

        def finalize(self, decision: str) -> str:
            calls.append(("finalize", decision))
            self.state.status = "completed"
            return "done"

    patch_build_headless_flow(monkeypatch, flow_module, FakeFlow)

    state = FlowState(
        request="resume it",
        target_repo="/tmp/fake",
        status="aborted_by_human",
        aborted_stage="do_work",
        spec="Implement resume support.",
        tasks=[
            TaskItem(
                id=1,
                title="Add resume path",
                description="Resume aborted flows from the CLI.",
            )
        ],
    )

    resumed = flow_module.resume_headless_flow(state)

    assert resumed.status == "completed"
    assert calls[0][0] == "do_work"
    assert "<spec>" in calls[0][1]
    assert "1. Add resume path" in calls[0][1]
    assert calls[1] == ("review", "work summary")
    assert calls[2] == ("finalize", "pass")


def test_resume_headless_flow_restarts_from_aborted_finalize(monkeypatch):
    calls: list[tuple[str, str]] = []

    class FakeFlow:
        def __init__(self, config=None, run_store=None):
            self.config = config
            self._run_store = run_store
            self._state = None

        @property
        def state(self):
            return self._state

        def _refresh_debug_report(self):
            return None

        def finalize(self, decision: str) -> str:
            assert self.state.status == "running"
            assert self.state.aborted_stage is None
            calls.append(("finalize", decision))
            self.state.status = "completed"
            return "done"

    patch_build_headless_flow(monkeypatch, flow_module, FakeFlow)

    state = FlowState(
        request="resume it",
        target_repo="/tmp/fake",
        status="aborted_by_human",
        aborted_stage="finalize",
    )

    resumed = flow_module.resume_headless_flow(state)

    assert resumed.status == "completed"
    assert calls == [("finalize", "pass")]


def test_resume_headless_flow_reopens_aborted_finalize_gate_and_can_rerun_review(
    monkeypatch,
):
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
            "review": "code-review-and-quality",
            "finalize": "documentation-and-adrs",
        },
        workers={
            "plan": {"worker": "codex"},
            "do_work": {"worker": "grok", "model": "grok-3-latest"},
            "review": {"worker": "codex", "sandbox": "read-only"},
            "finalize": {"worker": "codex"},
        },
        defaults={"worker": "codex", "timeout": 300},
        human_feedback={
            "enabled": True,
            "before_finalize": True,
            "action_allowlist": {"before_finalize": ["rerun-review", "skip-finalize"]},
        },
    )

    class FinalizeResumeAdapter:
        calls: list[dict[str, Any]] = []

        def run(self, task: str, cwd: str, mode: str = "edit", **kwargs):
            self.__class__.calls.append(
                {"task": task, "cwd": cwd, "mode": mode, **kwargs}
            )
            if mode == "inspect":
                return CoderResult(
                    summary="review complete",
                    raw_output='{"status": "pass", "issues": [], "summary": "Looks good"}',
                    exit_code=0,
                )
            return CoderResult(
                summary="finalized",
                changed_files=[],
                raw_output="finalized",
                exit_code=0,
            )

    state = FlowState(
        request="resume it",
        target_repo="/tmp/fake",
        status="aborted_by_human",
        aborted_stage="finalize",
        aborted_gate="before_finalize",
        aborted_stage_input="pass",
        latest_work_summary="work summary",
    )

    responses = iter(["rerun", "Focus on edge cases", "skip"])
    FinalizeResumeAdapter.calls = []
    monkeypatch.setitem(flow_module.WORKER_ADAPTERS, "codex", FinalizeResumeAdapter)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(responses))

    resumed = flow_module.resume_headless_flow(state, config=cfg)

    assert resumed.status == "completed"
    assert resumed.aborted_stage is None
    assert resumed.aborted_gate is None
    assert resumed.final_artifact == "Finalize skipped by human after review pass."
    assert resumed.review_status == "pass"
    assert resumed.latest_work_summary == "work summary"
    assert [entry.action for entry in resumed.human_feedback_log[-2:]] == [
        "rerun-review",
        "skip-finalize",
    ]
    assert [call["mode"] for call in FinalizeResumeAdapter.calls] == ["inspect"]
    assert FinalizeResumeAdapter.calls[0]["cwd"] == "/tmp/fake"


def test_resume_headless_flow_restarts_revision_loop_from_aborted_finalize(monkeypatch):
    calls: list[tuple[str, str]] = []

    class FakeFlow:
        def __init__(self, config=None, run_store=None):
            self.config = config
            self._run_store = run_store
            self._state = None

        @property
        def state(self):
            return self._state

        def _refresh_debug_report(self):
            return None

        def finalize(self, decision: str) -> str:
            calls.append(("finalize", decision))
            if len(calls) == 1:
                assert self.state.status == "running"
                assert self.state.aborted_stage is None
                return "revise"
            self.state.status = "completed"
            return "done"

        def revise(self, decision: str) -> str:
            calls.append(("revise", decision))
            return "fix prompt"

        def do_work(self, prompt: str) -> str:
            calls.append(("do_work", prompt))
            return "work summary"

        def review(self, work_summary: str) -> str:
            calls.append(("review", work_summary))
            return "pass"

    patch_build_headless_flow(monkeypatch, flow_module, FakeFlow)

    state = FlowState(
        request="resume it",
        target_repo="/tmp/fake",
        status="aborted_by_human",
        aborted_stage="finalize",
    )

    resumed = flow_module.resume_headless_flow(state)

    assert resumed.status == "completed"
    assert calls == [
        ("finalize", "pass"),
        ("revise", "revise"),
        ("do_work", "fix prompt"),
        ("review", "work summary"),
        ("finalize", "pass"),
    ]


def test_resume_headless_flow_stops_when_revise_marks_flow_failed(monkeypatch):
    calls: list[tuple[str, str]] = []

    class FakeFlow:
        def __init__(self, config=None, run_store=None):
            self.config = config
            self._run_store = run_store
            self._state = None

        @property
        def state(self):
            return self._state

        def _refresh_debug_report(self):
            return None

        def review(self, work_summary: str) -> str:
            calls.append(("review", work_summary))
            return "revise"

        def revise(self, decision: str) -> str:
            calls.append(("revise", decision))
            self.state.status = "failed"
            return "failed"

        def do_work(self, prompt: str) -> str:
            raise AssertionError(
                "do_work should not run after revise marks the flow failed"
            )

        def finalize(self, decision: str) -> str:
            raise AssertionError(
                "finalize should not run after revise marks the flow failed"
            )

    patch_build_headless_flow(monkeypatch, flow_module, FakeFlow)

    state = FlowState(
        request="resume it",
        target_repo="/tmp/fake",
        status="aborted_by_human",
        aborted_stage="review",
        aborted_stage_input="work summary",
    )

    resumed = flow_module.resume_headless_flow(state)

    assert resumed.status == "failed"
    assert calls == [("review", "work summary"), ("revise", "revise")]


def test_resume_headless_flow_restarts_from_aborted_plan(monkeypatch):
    calls: list[tuple[str, str | None]] = []

    class FakeFlow:
        def __init__(self, config=None, run_store=None):
            self.config = config
            self._run_store = run_store
            self._state = None

        @property
        def state(self):
            return self._state

        def _refresh_debug_report(self):
            return None

        def plan(self) -> str:
            assert self.state.status == "running"
            assert self.state.aborted_stage is None
            calls.append(("plan", None))
            return "<spec>\n1. Planned task"

        def do_work(self, plan_output: str) -> str:
            calls.append(("do_work", plan_output))
            return "work summary"

        def review(self, work_summary: str) -> str:
            calls.append(("review", work_summary))
            return "pass"

        def finalize(self, decision: str) -> str:
            calls.append(("finalize", decision))
            self.state.status = "completed"
            return "done"

    patch_build_headless_flow(monkeypatch, flow_module, FakeFlow)

    state = FlowState(
        request="resume it",
        target_repo="/tmp/fake",
        status="aborted_by_human",
        aborted_stage="plan",
    )

    resumed = flow_module.resume_headless_flow(state)

    assert resumed.status == "completed"
    assert calls == [
        ("plan", None),
        ("do_work", "<spec>\n1. Planned task"),
        ("review", "work summary"),
        ("finalize", "pass"),
    ]


def test_resume_headless_flow_restarts_from_aborted_review(monkeypatch):
    calls: list[tuple[str, str]] = []

    class FakeFlow:
        def __init__(self, config=None, run_store=None):
            self.config = config
            self._run_store = run_store
            self._state = None

        @property
        def state(self):
            return self._state

        def _refresh_debug_report(self):
            return None

        def review(self, work_summary: str) -> str:
            assert self.state.status == "running"
            assert self.state.aborted_stage is None
            calls.append(("review", work_summary))
            return "pass"

        def finalize(self, decision: str) -> str:
            calls.append(("finalize", decision))
            self.state.status = "completed"
            return "done"

    patch_build_headless_flow(monkeypatch, flow_module, FakeFlow)

    state = FlowState(
        request="resume it",
        target_repo="/tmp/fake",
        status="aborted_by_human",
        aborted_stage="review",
        aborted_stage_input="work summary",
    )

    resumed = flow_module.resume_headless_flow(state)

    assert resumed.status == "completed"
    assert calls == [("review", "work summary"), ("finalize", "pass")]


def test_resume_headless_flow_reopens_aborted_after_review_gate_without_rerunning_review(
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    class FakeFlow:
        def __init__(self, config=None, run_store=None):
            self.config = config
            self._run_store = run_store
            self._state = None

        @property
        def state(self):
            return self._state

        def _refresh_debug_report(self):
            return None

        def _resume_after_review_checkpoint(
            self,
            work_summary: str,
            *,
            saved_message: str | None = None,
            saved_before_review_instructions: str | None = None,
        ) -> str:
            assert self.state.status == "running"
            assert self.state.aborted_stage is None
            assert self.state.aborted_gate_message is None
            assert self.state.aborted_before_review_instructions is None
            assert saved_message == "Automated review completed."
            assert saved_before_review_instructions == "Focus on migrations"
            calls.append(("after_review", work_summary))
            return "pass"

        def review(self, work_summary: str) -> str:
            raise AssertionError(
                "review worker should not rerun for after_review resume"
            )

        def finalize(self, decision: str) -> str:
            calls.append(("finalize", decision))
            self.state.status = "completed"
            return "done"

    patch_build_headless_flow(monkeypatch, flow_module, FakeFlow)

    state = FlowState(
        request="resume it",
        target_repo="/tmp/fake",
        status="aborted_by_human",
        aborted_stage="review",
        aborted_gate="after_review",
        aborted_gate_message="Automated review completed.",
        aborted_before_review_instructions="Focus on migrations",
        aborted_stage_input="work summary",
        review_status="revise",
        issues=["Need more tests"],
    )

    resumed = flow_module.resume_headless_flow(state)

    assert resumed.status == "completed"
    assert calls == [("after_review", "work summary"), ("finalize", "pass")]


def test_resume_after_review_checkpoint_falls_back_to_feedback_log_message(
    monkeypatch,
):
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
            "review": "code-review-and-quality",
            "finalize": "documentation-and-adrs",
        },
        workers={
            "plan": {"worker": "codex"},
            "do_work": {"worker": "codex", "always_approve": True},
            "review": {"worker": "codex", "sandbox": "read-only"},
            "finalize": {"worker": "codex"},
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="resume it",
        target_repo="/tmp/fake",
        review_status="revise",
        human_feedback_log=[
            HumanFeedbackEntry(
                stage="review",
                gate="after_review",
                approved=False,
                action="abort",
                response="n",
                instructions=None,
                revision=0,
                worker="codex",
                skill="code-review-and-quality",
                can_mutate=False,
                message="Automated review completed from the legacy log.",
            )
        ],
    )

    calls: list[dict[str, str | None]] = []

    def fake_handle_after_review_checkpoint(
        *,
        work_summary: str,
        message: str,
        automated_status: str,
        human_guidance: str | None = None,
    ) -> tuple[str, None]:
        calls.append(
            {
                "work_summary": work_summary,
                "message": message,
                "automated_status": automated_status,
                "human_guidance": human_guidance,
            }
        )
        return "pass", None

    monkeypatch.setattr(
        flow, "_handle_after_review_checkpoint", fake_handle_after_review_checkpoint
    )

    result = flow._resume_after_review_checkpoint("work summary")

    assert result == "pass"
    assert calls == [
        {
            "work_summary": "work summary",
            "message": "Automated review completed from the legacy log.",
            "automated_status": "revise",
            "human_guidance": None,
        }
    ]


def test_resume_after_review_checkpoint_falls_back_to_feedback_log_before_review_instructions(
    monkeypatch,
):
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
            "review": "code-review-and-quality",
            "finalize": "documentation-and-adrs",
        },
        workers={
            "plan": {"worker": "codex"},
            "do_work": {"worker": "codex", "always_approve": True},
            "review": {"worker": "codex", "sandbox": "read-only"},
            "finalize": {"worker": "codex"},
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="resume it",
        target_repo="/tmp/fake",
        review_status="revise",
        human_feedback_log=[
            HumanFeedbackEntry(
                stage="review",
                gate="before_review",
                approved=True,
                action="approve",
                response="y",
                instructions="Focus on migrations",
                revision=0,
                worker="codex",
                skill="code-review-and-quality",
                can_mutate=False,
                message="About to run read-only review stage.",
            ),
            HumanFeedbackEntry(
                stage="review",
                gate="after_review",
                approved=False,
                action="rerun-review",
                response="rerun",
                instructions="Focus on edge cases",
                revision=0,
                worker="codex",
                skill="code-review-and-quality",
                can_mutate=False,
                message="Automated review completed from the legacy log.",
            ),
        ],
    )

    after_calls: list[dict[str, str | None]] = []
    review_calls: list[dict[str, str | None]] = []

    def fake_handle_after_review_checkpoint(
        *,
        work_summary: str,
        message: str,
        automated_status: str,
        human_guidance: str | None = None,
    ) -> tuple[str, str | None]:
        after_calls.append(
            {
                "work_summary": work_summary,
                "message": message,
                "automated_status": automated_status,
                "human_guidance": human_guidance,
            }
        )
        if len(after_calls) == 1:
            return "rerun-review", "Focus on edge cases"
        return "pass", None

    def fake_run_automated_review_once(
        *,
        work_summary: str,
        human_guidance: str | None = None,
        review_rerun_guidance: str | None = None,
        verification_evidence: str | None = None,
    ) -> ReviewCrewDecision:
        review_calls.append(
            {
                "work_summary": work_summary,
                "human_guidance": human_guidance,
                "review_rerun_guidance": review_rerun_guidance,
            }
        )
        return ReviewCrewDecision(
            status="pass",
            issues=[],
            summary="looks good now",
        )

    monkeypatch.setattr(
        flow, "_handle_after_review_checkpoint", fake_handle_after_review_checkpoint
    )
    monkeypatch.setattr(
        flow, "_run_automated_review_once", fake_run_automated_review_once
    )

    result = flow._resume_after_review_checkpoint("work summary")

    assert result == "pass"
    assert after_calls[0]["human_guidance"] == "Focus on migrations"
    assert review_calls == [
        {
            "work_summary": "work summary",
            "human_guidance": "Focus on migrations",
            "review_rerun_guidance": "Focus on edge cases",
        }
    ]


def test_resume_headless_flow_refreshes_runtime_snapshot_from_supplied_config(
    monkeypatch,
):
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
            "review": "code-review-and-quality",
            "finalize": "documentation-and-adrs",
        },
        workers={
            "plan": {"worker": "codex", "model": "gpt-4.1-mini"},
            "do_work": {
                "worker": "codex",
                "model": "gpt-4.1",
                "always_approve": True,
                "parallel": {"enabled": True, "max_workers": 3},
            },
            "review": {
                "worker": "codex",
                "sandbox": "read-only",
                "crew": {"enabled": True, "process": "sequential"},
            },
            "finalize": {"worker": "codex", "model": "gpt-4.1-mini"},
        },
        defaults={"worker": "codex", "timeout": 300},
        human_feedback={
            "enabled": True,
            "after_review": True,
            "action_allowlist": {"after_review": ["force-pass", "replan"]},
        },
    )
    state = FlowState(
        request="resume it",
        target_repo="/tmp/fake",
        status="aborted_by_human",
        aborted_stage="finalize",
        aborted_stage_input="pass",
        resolved_stages=[],
        resolved_human_feedback={},
        debug_report=None,
    )

    FinalizeOnlyAdapter.calls = []
    monkeypatch.setitem(flow_module.WORKER_ADAPTERS, "codex", FinalizeOnlyAdapter)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    resumed = flow_module.resume_headless_flow(state, config=cfg)

    assert resumed.status == "completed"
    assert resumed.aborted_stage is None
    assert resumed.aborted_gate is None
    assert resumed.aborted_gate_message is None
    assert resumed.aborted_before_review_instructions is None
    assert resumed.aborted_stage_input is None
    assert len(resumed.resolved_stages) == 4
    do_work = next(
        stage for stage in resumed.resolved_stages if stage.stage == "do_work"
    )
    review = next(stage for stage in resumed.resolved_stages if stage.stage == "review")
    assert do_work.model == "gpt-4.1"
    assert do_work.runtime_knobs == {"parallel": {"enabled": True, "max_workers": 3}}
    assert do_work.enforced_declarations == {"always_approve": True}
    assert review.runtime_knobs == {"crew": {"enabled": True}}
    assert review.enforced_declarations == {
        "sandbox": "read-only",
        "crew": {"process": "sequential"},
    }
    assert resumed.resolved_human_feedback["enabled"] is True
    assert resumed.resolved_human_feedback["after_review"] is True
    assert resumed.resolved_human_feedback["action_allowlist"] == {
        "after_review": ["force-pass", "replan"]
    }
    assert resumed.debug_report is not None
    assert "## Runtime Configuration" in resumed.debug_report
    assert FinalizeOnlyAdapter.calls[0]["model"] == "gpt-4.1-mini"


def test_review_crew_path_is_used_when_enabled(monkeypatch):
    cfg = FlowConfig(
        skills={"review": "code-review-and-quality"},
        workers={
            "review": {
                "worker": "codex",
                "model": "sonnet",
                "sandbox": "read-only",
                "crew": {"enabled": True, "process": "sequential"},
            }
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow._workers["review"] = StubWorker(review_outcome="pass")  # type: ignore

    calls: list[dict] = []

    def fake_run_review_crew(**kwargs):
        calls.append(kwargs)
        return ReviewCrewDecision(
            status="pass",
            issues=[],
            summary="Crew approved.",
        )

    monkeypatch.setattr(
        "crewai_headless_flow.stages.review.run_review_crew",
        fake_run_review_crew,
    )

    decision = flow.review("work summary")

    assert decision == "pass"
    assert flow.state.review_status == "pass"
    assert calls
    assert calls[0]["worker_tool"] is flow._workers["review"]
    assert calls[0]["model"] == "sonnet"


def test_review_crew_path_fails_closed_to_revise(monkeypatch):
    cfg = FlowConfig(
        skills={"review": "code-review-and-quality"},
        workers={
            "review": {
                "worker": "codex",
                "sandbox": "read-only",
                "crew": {"enabled": True, "process": "sequential"},
            }
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow._workers["review"] = StubWorker(review_outcome="pass")  # type: ignore

    def fake_run_review_crew(**kwargs):
        raise RuntimeError("crew unavailable")

    monkeypatch.setattr(
        "crewai_headless_flow.stages.review.run_review_crew",
        fake_run_review_crew,
    )

    decision = flow.review("work summary")

    assert decision == "revise"
    assert flow.state.review_status == "revise"
    assert flow.state.issues == ["Review Crew failed: crew unavailable"]


# =============================================================================
# Objective verification gate (autonomy Gap 1)
# =============================================================================


class FakeVerifyRunner:
    """Scripted subprocess stand-in for the Flow's verification seam."""

    def __init__(
        self, outcomes: dict[tuple[str, ...], tuple[int, str, str]] | None = None
    ):
        self.outcomes = outcomes or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, cwd, capture_output, text, timeout, check):
        import subprocess as _subprocess

        self.calls.append(list(argv))
        exit_code, stdout, stderr = self.outcomes.get(tuple(argv), (0, "ok", ""))
        return _subprocess.CompletedProcess(argv, exit_code, stdout, stderr)


def _verify_config(commands: list, mode: str = "gate") -> FlowConfig:
    return FlowConfig(
        skills={"review": "code-review-and-quality"},
        workers={"review": {"worker": "codex"}},
        defaults={"worker": "codex", "timeout": 300},
        verify={"commands": commands, "mode": mode},
    )


def test_verification_gate_failure_skips_llm_review_and_routes_to_revise():
    flow = CrewAIHeadlessFlow(config=_verify_config(["pytest -q"]))
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    runner = FakeVerifyRunner(
        outcomes={("pytest", "-q"): (1, "2 failed, 5 passed", "")}
    )
    flow._verification_runner = runner
    worker = RecordingWorker()
    flow._workers["review"] = worker  # type: ignore

    decision = flow.review("work summary")

    assert decision == "revise"
    assert worker.calls == []  # LLM review never ran
    assert flow.state.review_status == "revise"
    assert len(flow.state.issues) == 1
    assert "`pytest -q` exited 1" in flow.state.issues[0]
    assert "2 failed" in flow.state.issues[0]
    assert len(flow.state.verification_runs) == 1
    report = flow.state.verification_runs[0]
    assert report.passed is False
    assert report.revision == flow.state.revisions
    assert flow.state.history[-1].kind == "review_decision"


def test_verification_gate_pass_falls_through_to_llm_review():
    flow = CrewAIHeadlessFlow(config=_verify_config(["pytest -q"]))
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    runner = FakeVerifyRunner()
    flow._verification_runner = runner
    worker = RecordingWorker()
    flow._workers["review"] = worker  # type: ignore

    decision = flow.review("work summary")

    assert decision == "pass"
    assert len(worker.calls) == 1
    assert runner.calls == [["pytest", "-q"]]
    assert flow.state.verification_runs[0].passed is True
    # Passing evidence is offered to the reviewer as context.
    assert "Objective verification evidence" in worker.calls[0]["task"]


def test_verification_advisory_failure_still_runs_llm_review_with_evidence():
    flow = CrewAIHeadlessFlow(config=_verify_config(["pytest -q"], mode="advisory"))
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    runner = FakeVerifyRunner(
        outcomes={("pytest", "-q"): (1, "1 failed", "stderr noise")}
    )
    flow._verification_runner = runner
    worker = RecordingWorker()
    flow._workers["review"] = worker  # type: ignore

    decision = flow.review("work summary")

    assert decision == "pass"  # advisory: the LLM still decides
    assert len(worker.calls) == 1
    prompt = worker.calls[0]["task"]
    assert "Objective verification evidence" in prompt
    assert "`pytest -q` — exit 1" in prompt
    assert "1 failed" in prompt
    assert flow.state.verification_runs[0].mode == "advisory"


def test_verification_unconfigured_never_calls_runner():
    flow = CrewAIHeadlessFlow()
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    runner = FakeVerifyRunner()
    flow._verification_runner = runner
    flow._workers["review"] = StubWorker(review_outcome="pass")  # type: ignore

    decision = flow.review("work summary")

    assert decision == "pass"
    assert runner.calls == []
    assert flow.state.verification_runs == []


def test_verification_reruns_every_review_round():
    flow = CrewAIHeadlessFlow(config=_verify_config(["pytest -q"]))
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    runner = FakeVerifyRunner()
    flow._verification_runner = runner
    flow._workers["review"] = StubWorker(review_outcome="pass")  # type: ignore

    flow.review("round one")
    flow.review("round two")

    assert len(runner.calls) == 2
    assert len(flow.state.verification_runs) == 2


def test_verification_gate_failure_revision_stamp_tracks_state():
    flow = CrewAIHeadlessFlow(config=_verify_config(["pytest -q"]))
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test", target_repo="/tmp/fake", revisions=1
    )
    runner = FakeVerifyRunner(outcomes={("pytest", "-q"): (1, "fail", "")})
    flow._verification_runner = runner
    flow._workers["review"] = RecordingWorker()  # type: ignore

    flow.review("work summary")

    assert flow.state.verification_runs[0].revision == 1


def test_human_force_pass_skips_verification_round():
    cfg = FlowConfig(
        skills={"review": "code-review-and-quality"},
        workers={"review": {"worker": "codex"}},
        defaults={"worker": "codex", "timeout": 300},
        human_feedback={
            "enabled": True,
            "before_review": True,
            "advanced_actions": True,
        },
        verify={"commands": ["pytest -q"], "mode": "gate"},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    runner = FakeVerifyRunner(outcomes={("pytest", "-q"): (1, "fail", "")})
    flow._verification_runner = runner
    worker = RecordingWorker()
    flow._workers["review"] = worker  # type: ignore

    from unittest.mock import patch as _patch

    with _patch("builtins.input", return_value="pass"):
        decision = flow.review("work summary")

    assert decision == "pass"
    assert runner.calls == []  # human override is sovereign at review time
    assert worker.calls == []
    assert flow.state.verification_runs == []


def test_finalize_gate_rerun_review_reverifies_the_tree(monkeypatch):
    # A rerun-review at the finalize gate must not produce a review decision
    # against an unverified tree.
    cfg = FlowConfig(
        skills={
            "review": "code-review-and-quality",
            "finalize": "documentation-and-adrs",
        },
        workers={"review": {"worker": "codex"}, "finalize": {"worker": "codex"}},
        defaults={"worker": "codex", "timeout": 300},
        human_feedback={
            "enabled": True,
            "before_finalize": True,
            "action_allowlist": {"before_finalize": ["rerun-review"]},
        },
        verify={"commands": ["pytest -q"], "mode": "gate"},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        review_status="pass",
        latest_work_summary="work summary",
    )
    runner = FakeVerifyRunner(outcomes={("pytest", "-q"): (1, "2 failed", "")})
    flow._verification_runner = runner
    review_worker = RecordingWorker()
    flow._workers["review"] = review_worker  # type: ignore
    flow._workers["finalize"] = RecordingWorker()  # type: ignore

    responses = iter(["rerun", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(responses))

    result = cast(Any, flow).finalize("pass")

    assert result == "revise"
    assert runner.calls == [["pytest", "-q"]]  # the rerun re-verified
    assert review_worker.calls == []  # gate failure skipped the LLM review
    assert len(flow.state.verification_runs) == 1
    assert flow.state.verification_runs[0].passed is False


def test_resume_rerun_review_reverifies_the_tree(monkeypatch):
    flow = CrewAIHeadlessFlow(config=_verify_config(["pytest -q"]))
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        review_status="revise",
    )
    runner = FakeVerifyRunner()
    flow._verification_runner = runner
    worker = RecordingWorker()
    flow._workers["review"] = worker  # type: ignore

    outcomes = iter([("rerun-review", "look again"), ("pass", None)])
    monkeypatch.setattr(
        flow, "_handle_after_review_checkpoint", lambda **kwargs: next(outcomes)
    )

    result = flow._resume_after_review_checkpoint(
        "work summary", saved_message="Automated review returned revise."
    )

    assert result == "pass"
    assert runner.calls == [["pytest", "-q"]]  # the rerun re-verified
    assert len(worker.calls) == 1  # verification passed, so the LLM review ran


def test_verification_round_checkpoints_before_running_commands():
    flow = CrewAIHeadlessFlow(config=_verify_config(["pytest -q"]))
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    class RecordingRunStore:
        def __init__(self):
            self.states: list[str] = []

        def save_state(self, payload: str) -> None:
            self.states.append(payload)

        def save_debug_report(self, payload: str) -> None:
            pass

        def append_event(self, payload: str) -> None:
            pass

    store = RecordingRunStore()
    flow._run_store = store  # type: ignore

    stages_at_run_time: list[object] = []

    def runner(argv, **kwargs):
        import subprocess as _subprocess

        stage = json.loads(store.states[-1]).get("last_stage") if store.states else None
        stages_at_run_time.append(stage)
        return _subprocess.CompletedProcess(argv, 0, "ok", "")

    flow._verification_runner = runner
    flow._workers["review"] = StubWorker(review_outcome="pass")  # type: ignore

    flow.review("work summary")

    # A crash mid-verify must resume at the review stage: the checkpoint
    # written BEFORE the commands ran already carries last_stage="review".
    assert stages_at_run_time == ["review"]


# =============================================================================
# WorkerSpec / configurable binaries (autonomy Gap 10)
# =============================================================================


def test_setup_workers_passes_configured_binary(monkeypatch):
    captured: list[dict] = []

    class SpyAdapter:
        def __init__(self, binary: str = "default-bin"):
            captured.append({"binary": binary})

        def run(self, **kwargs):
            raise AssertionError("not used")

    monkeypatch.setitem(flow_module.WORKER_ADAPTERS, "claude", SpyAdapter)

    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"do_work": {"worker": "claude"}},
        defaults={"worker": "claude", "timeout": 300},
        worker_settings={"claude": {"binary": "/opt/bin/claude-nightly"}},
    )
    CrewAIHeadlessFlow(config=cfg)

    assert {"binary": "/opt/bin/claude-nightly"} in captured


def test_setup_workers_uses_adapter_default_without_override(monkeypatch):
    captured: list[dict] = []

    class SpyAdapter:
        def __init__(self, binary: str = "default-bin"):
            captured.append({"binary": binary})

        def run(self, **kwargs):
            raise AssertionError("not used")

    monkeypatch.setitem(flow_module.WORKER_ADAPTERS, "claude", SpyAdapter)

    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"do_work": {"worker": "claude"}},
        defaults={"worker": "claude", "timeout": 300},
    )
    CrewAIHeadlessFlow(config=cfg)

    assert {"binary": "default-bin"} in captured


def test_fallback_worker_honors_configured_binary(monkeypatch):
    captured: list[dict] = []

    class SpyAdapter:
        def __init__(self, binary: str = "default-bin"):
            captured.append({"binary": binary})

        def run(self, **kwargs):
            raise AssertionError("not used")

    monkeypatch.setitem(flow_module.WORKER_ADAPTERS, "gemini", SpyAdapter)

    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"do_work": {"worker": "claude", "fallback_worker": "gemini"}},
        defaults={"worker": "claude", "timeout": 300},
        worker_settings={"gemini": {"binary": "/opt/bin/gemini-nightly"}},
    )
    CrewAIHeadlessFlow(config=cfg)

    assert {"binary": "/opt/bin/gemini-nightly"} in captured
