"""
Gap 8(a) (autonomy Phase 1): mergeback path sanitization.

Worker-reported changed_files are untrusted input. apply_changed_files must
refuse absolute paths and parent traversal — validating the whole list before
touching disk — and the Flow's parallel mergeback must convert that refusal
into an ordinary task failure instead of crashing the run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from crewai_headless_flow.config import FlowConfig
from crewai_headless_flow.flow import CrewAIHeadlessFlow
from crewai_headless_flow.state import FlowState, TaskItem
from crewai_headless_flow.workers.base import CoderResult
from crewai_headless_flow.workspace_changes import apply_changed_files


pytestmark = pytest.mark.offline


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    src_root = tmp_path / "workspace"
    dest_root = tmp_path / "repo"
    src_root.mkdir()
    dest_root.mkdir()
    return src_root, dest_root


def test_apply_changed_files_copies_and_deletes_normal_paths(tmp_path: Path):
    src_root, dest_root = _roots(tmp_path)
    (src_root / "src").mkdir()
    (src_root / "src/new.py").write_text("new\n")
    (dest_root / "stale.py").write_text("stale\n")

    apply_changed_files(
        src_root=src_root,
        dest_root=dest_root,
        changed_files=["src/new.py", "stale.py"],
    )

    assert (dest_root / "src/new.py").read_text() == "new\n"
    assert not (dest_root / "stale.py").exists()


def test_apply_changed_files_rejects_absolute_path(tmp_path: Path):
    src_root, dest_root = _roots(tmp_path)
    outside = tmp_path / "outside.txt"

    with pytest.raises(ValueError, match="Refusing to apply changed file"):
        apply_changed_files(
            src_root=src_root,
            dest_root=dest_root,
            changed_files=[str(outside)],
        )


def test_apply_changed_files_rejects_parent_traversal(tmp_path: Path):
    src_root, dest_root = _roots(tmp_path)

    with pytest.raises(ValueError, match="Refusing to apply changed file"):
        apply_changed_files(
            src_root=src_root,
            dest_root=dest_root,
            changed_files=["../evil.txt"],
        )
    assert not (tmp_path / "evil.txt").exists()


def test_apply_changed_files_rejects_nested_traversal(tmp_path: Path):
    src_root, dest_root = _roots(tmp_path)

    with pytest.raises(ValueError, match="Refusing to apply changed file"):
        apply_changed_files(
            src_root=src_root,
            dest_root=dest_root,
            changed_files=["src/../../evil.txt"],
        )


def test_apply_changed_files_rejects_symlink_escape(tmp_path: Path):
    src_root, dest_root = _roots(tmp_path)
    escape_target = tmp_path / "elsewhere"
    escape_target.mkdir()
    (dest_root / "link").symlink_to(escape_target, target_is_directory=True)
    (src_root / "link").mkdir()
    (src_root / "link/file.txt").write_text("escaped\n")

    with pytest.raises(ValueError, match="Refusing to apply changed file"):
        apply_changed_files(
            src_root=src_root,
            dest_root=dest_root,
            changed_files=["link/file.txt"],
        )
    assert not (escape_target / "file.txt").exists()


def test_apply_changed_files_poisoned_list_applies_nothing(tmp_path: Path):
    """One bad entry must prevent the whole batch — no partial application."""
    src_root, dest_root = _roots(tmp_path)
    (src_root / "good.py").write_text("good\n")

    with pytest.raises(ValueError, match="Refusing to apply changed file"):
        apply_changed_files(
            src_root=src_root,
            dest_root=dest_root,
            changed_files=["good.py", "../evil.txt"],
        )

    assert not (dest_root / "good.py").exists()


class EscapingWorker:
    """Task 1 self-reports a traversal path; task 2 edits normally."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, **kwargs) -> CoderResult:
        self.calls.append(kwargs)
        cwd = Path(kwargs["cwd"])
        task_id = 1 if "src/a.py" in kwargs["task"] else 2
        if task_id == 1:
            (cwd / "src/a.py").write_text("after a\n")
            return CoderResult(
                summary="task 1 complete",
                changed_files=["src/a.py", "../evil.txt"],
                raw_output="task 1 complete",
                exit_code=0,
            )
        (cwd / "src/b.py").write_text("after b\n")
        return CoderResult(
            summary="task 2 complete",
            changed_files=["src/b.py"],
            raw_output="task 2 complete",
            exit_code=0,
        )


def test_parallel_mergeback_rejects_unsafe_path_as_task_failure(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/a.py").write_text("before a\n")
    (repo / "src/b.py").write_text("before b\n")
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={
            "do_work": {
                "worker": "claude",
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
    flow._workers["do_work"] = EscapingWorker()  # type: ignore

    cast(Any, flow).do_work("plan output")

    assert flow.state.tasks[0].status == "failed"
    assert "Mergeback rejected unsafe path" in (flow.state.tasks[0].last_error or "")
    assert not (tmp_path / "evil.txt").exists()
    # Task 1's legitimate edit stays quarantined in its workspace copy too —
    # the poisoned list rejects the whole outcome.
    assert (repo / "src/a.py").read_text() == "before a\n"
    # Task 2's clean outcome still merges.
    assert flow.state.tasks[1].status == "done"
    assert (repo / "src/b.py").read_text() == "after b\n"
