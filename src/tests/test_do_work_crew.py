from __future__ import annotations

from types import SimpleNamespace

import pytest

from crewai_headless_flow.do_work_contract import DoWorkExecutionPlan, DoWorkSubtask
from crewai_headless_flow.do_work_crew import (
    DEFAULT_MAX_ROUNDS,
    DEFAULT_MAX_SUBTASKS,
    HeadlessEditTool,
    _max_subtasks,
    _max_rounds,
    normalize_do_work_crew_output,
    run_do_work_crew,
)
from crewai_headless_flow.review_contract import ReviewDecision
from crewai_headless_flow.workers.base import CoderResult


pytestmark = pytest.mark.offline


class RecordingWorkerTool:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return CoderResult(
            summary="implemented",
            raw_output="implementation details",
            exit_code=0,
        )


def test_normalize_do_work_crew_output_accepts_pydantic_result():
    crew_output = SimpleNamespace(
        pydantic=ReviewDecision(
            status="pass",
            issues=[],
            summary="Task is complete.",
            task_hints=[],
        )
    )

    decision = normalize_do_work_crew_output(crew_output)

    assert decision.status == "pass"
    assert decision.summary == "Task is complete."


def test_normalize_do_work_crew_output_fails_closed_on_malformed_output():
    decision = normalize_do_work_crew_output("not json")

    assert decision.status == "revise"
    assert decision.issues == ["Implementation Crew output could not be parsed"]


def test_headless_edit_tool_always_calls_edit_mode():
    worker_tool = RecordingWorkerTool()
    tool = HeadlessEditTool(
        worker_tool=worker_tool,
        cwd="/tmp/repo",
        timeout=23,
        model="sonnet",
    )

    result = tool._run(prompt="Implement this task")

    assert result == "implemented"
    assert worker_tool.calls == [
        {
            "task": "Implement this task",
            "cwd": "/tmp/repo",
            "mode": "edit",
            "timeout": 23,
            "model": "sonnet",
        }
    ]


def test_run_do_work_crew_retries_after_revise_then_passes(monkeypatch):
    calls: list[str] = []

    def fake_round(**kwargs):
        calls.append(kwargs["task_prompt"])
        if len(calls) == 1:
            return (
                CoderResult(summary="round 1", raw_output="round 1", exit_code=0),
                ReviewDecision(
                    status="revise",
                    issues=["Add missing verification"],
                    summary="Needs another pass.",
                ),
            )
        return (
            CoderResult(summary="round 2", raw_output="round 2", exit_code=0),
            ReviewDecision(
                status="pass",
                issues=[],
                summary="Task is complete.",
            ),
        )

    monkeypatch.setattr(
        "crewai_headless_flow.do_work_crew._run_do_work_crew_round",
        fake_round,
    )

    result, decision = run_do_work_crew(
        task_prompt="Implement task 1",
        worker_tool=object(),  # not used by fake round
        cwd="/tmp/repo",
        timeout=30,
        crew_config={"max_rounds": 2},
    )

    assert len(calls) == 2
    assert calls[0] == "Implement task 1"
    assert "Previous implementation round 1 requested revision." in calls[1]
    assert "Add missing verification" in calls[1]
    assert result.summary == "round 2"
    assert decision.status == "pass"


def test_run_do_work_crew_returns_last_revise_when_rounds_exhausted(monkeypatch):
    calls: list[str] = []

    def fake_round(**kwargs):
        calls.append(kwargs["task_prompt"])
        return (
            CoderResult(
                summary=f"round {len(calls)}",
                raw_output=f"round {len(calls)}",
                exit_code=0,
            ),
            ReviewDecision(
                status="revise",
                issues=[f"Issue {len(calls)}"],
                summary=f"Needs round {len(calls)}.",
            ),
        )

    monkeypatch.setattr(
        "crewai_headless_flow.do_work_crew._run_do_work_crew_round",
        fake_round,
    )

    result, decision = run_do_work_crew(
        task_prompt="Implement task 1",
        worker_tool=object(),  # not used by fake round
        cwd="/tmp/repo",
        timeout=30,
        crew_config={"max_rounds": 2},
    )

    assert len(calls) == 2
    assert result.summary == "round 2"
    assert decision.status == "revise"
    assert decision.issues == ["Issue 2"]


def test_run_do_work_crew_uses_decomposition_plan_and_executes_each_subtask(
    monkeypatch,
):
    prompts: list[str] = []
    observer_events: list[dict] = []

    monkeypatch.setattr(
        "crewai_headless_flow.do_work_crew._run_do_work_decomposition_crew",
        lambda **kwargs: DoWorkExecutionPlan(
            summary="Split task into two slices.",
            subtasks=[
                DoWorkSubtask(
                    id=1,
                    title="Add contract",
                    description="Create contract types.",
                    files=["src/contract.py"],
                    verification=["pytest -q"],
                ),
                DoWorkSubtask(
                    id=2,
                    title="Wire flow",
                    description="Use contract in flow.",
                    files=["src/flow.py"],
                    verification=["pytest -q"],
                ),
            ],
        ),
    )

    def fake_round(**kwargs):
        prompts.append(kwargs["task_prompt"])
        subtask_id = 1 if "Add contract" in kwargs["task_prompt"] else 2
        return (
            CoderResult(summary=f"round {subtask_id}", raw_output="", exit_code=0),
            ReviewDecision(
                status="pass",
                issues=[],
                summary=f"Subtask {subtask_id} looks good.",
            ),
        )

    monkeypatch.setattr(
        "crewai_headless_flow.do_work_crew._run_do_work_crew_round",
        fake_round,
    )

    result, decision = run_do_work_crew(
        task_prompt="Implement task 1",
        worker_tool=object(),
        cwd="/tmp/repo",
        timeout=30,
        crew_config={"max_rounds": 2, "decomposition": {"enabled": True}},
        round_observer=observer_events.append,
    )

    assert len(prompts) == 2
    assert "Implementation slice 1/2" in prompts[0]
    assert "Add contract" in prompts[0]
    assert "Implementation slice 2/2" in prompts[1]
    assert "Wire flow" in prompts[1]
    assert observer_events[0]["subtask_id"] == 1
    assert observer_events[0]["subtask_title"] == "Add contract"
    assert observer_events[1]["subtask_id"] == 2
    assert "Subtask 1 complete" in result.summary
    assert "Subtask 2 complete" in result.summary
    assert decision.status == "pass"


def test_run_do_work_crew_falls_back_to_single_task_when_decomposition_invalid(
    monkeypatch,
):
    prompts: list[str] = []

    monkeypatch.setattr(
        "crewai_headless_flow.do_work_crew._run_do_work_decomposition_crew",
        lambda **kwargs: None,
    )

    def fake_round(**kwargs):
        prompts.append(kwargs["task_prompt"])
        return (
            CoderResult(summary="round 1", raw_output="", exit_code=0),
            ReviewDecision(
                status="pass",
                issues=[],
                summary="Task is complete.",
            ),
        )

    monkeypatch.setattr(
        "crewai_headless_flow.do_work_crew._run_do_work_crew_round",
        fake_round,
    )

    result, decision = run_do_work_crew(
        task_prompt="Implement task 1",
        worker_tool=object(),
        cwd="/tmp/repo",
        timeout=30,
        crew_config={"decomposition": {"enabled": True}},
    )

    assert prompts == ["Implement task 1"]
    assert result.summary == "round 1"
    assert decision.summary == "Task is complete."


def test_max_rounds_defaults_and_rejects_non_positive_values():
    assert _max_rounds({}) == DEFAULT_MAX_ROUNDS
    assert _max_rounds({"max_rounds": 3}) == 3

    with pytest.raises(
        ValueError, match="Implementation Crew max_rounds must be at least 1"
    ):
        _max_rounds({"max_rounds": 0})


def test_max_subtasks_defaults_and_rejects_non_positive_values():
    assert _max_subtasks({}) == DEFAULT_MAX_SUBTASKS
    assert _max_subtasks({"decomposition": {"max_subtasks": 3}}) == 3

    with pytest.raises(
        ValueError,
        match="Implementation Crew decomposition max_subtasks must be at least 1",
    ):
        _max_subtasks({"decomposition": {"max_subtasks": 0}})
