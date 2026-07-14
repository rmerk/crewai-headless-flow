"""
Gap 8b (autonomy Phase 2): deny-path enforcement + serial do_work isolation.

Unit tests pin the glob/restore semantics; flow tests drive all four
Flow-owned enforcement boundaries (parallel mergeback, serial in-place,
unstructured edit, finalize diff) with tmp_path git repos and worker doubles.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from crewai_headless_flow.config import FlowConfig
from crewai_headless_flow.flow import CrewAIHeadlessFlow
from crewai_headless_flow.paths_policy import match_denied, restore_denied_paths
from crewai_headless_flow.state import FlowState, TaskItem
from crewai_headless_flow.workers.base import CoderResult


pytestmark = pytest.mark.offline


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _git_repo(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True)
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    for rel_path, content in (files or {"README.md": "# repo\n"}).items():
        target = repo / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial commit")
    return repo


# --- match_denied ------------------------------------------------------------


def test_match_denied_star_crosses_slashes():
    # Broad-by-design safety semantics: `*` crosses `/`. Do not "fix" this
    # into a bypass.
    matches = match_denied(["x.env", "sub/dir/x.env", "src/a.py"], ["*.env"])

    assert matches == {"x.env": "*.env", "sub/dir/x.env": "*.env"}


def test_match_denied_directory_glob_matches_nested():
    matches = match_denied(
        ["secrets/key.pem", "secrets/deep/nested.pem", "src/a.py"], ["secrets/*"]
    )

    assert set(matches) == {"secrets/key.pem", "secrets/deep/nested.pem"}


def test_match_denied_reports_first_matching_pattern():
    matches = match_denied(["a.env"], ["*.env", "a.*"])

    assert matches == {"a.env": "*.env"}


def test_match_denied_is_case_sensitive():
    assert match_denied(["X.ENV"], ["*.env"]) == {}


def test_match_denied_empty_deny_matches_nothing():
    assert match_denied(["a.env", "b.py"], []) == {}


# --- restore_denied_paths ------------------------------------------------------


def test_restore_tracked_file_reverts_content(tmp_path: Path):
    repo = _git_repo(tmp_path, {"config.yaml": "original\n"})
    (repo / "config.yaml").write_text("tampered\n")

    unrestorable = restore_denied_paths(repo, ["config.yaml"])

    assert unrestorable == []
    assert (repo / "config.yaml").read_text() == "original\n"


def test_restore_unlinks_run_created_untracked_file(tmp_path: Path):
    repo = _git_repo(tmp_path)
    (repo / "leak.env").write_text("SECRET=1\n")

    unrestorable = restore_denied_paths(repo, ["leak.env"], created=["leak.env"])

    assert unrestorable == []
    assert not (repo / "leak.env").exists()


def test_restore_leaves_preexisting_untracked_file_and_reports_it(tmp_path: Path):
    repo = _git_repo(tmp_path)
    # Pre-existed the run (not in `created`), untracked, modified in place:
    # deleting it would destroy operator data.
    (repo / "operator.env").write_text("worker-tampered\n")

    unrestorable = restore_denied_paths(repo, ["operator.env"], created=[])

    assert unrestorable == ["operator.env"]
    assert (repo / "operator.env").read_text() == "worker-tampered\n"


def test_restore_reports_deleted_preexisting_untracked_file(tmp_path: Path):
    repo = _git_repo(tmp_path)

    unrestorable = restore_denied_paths(repo, ["gone.env"], created=[])

    assert unrestorable == ["gone.env"]


def test_restore_tracked_deleted_file_is_recovered(tmp_path: Path):
    repo = _git_repo(tmp_path, {"keep.txt": "content\n"})
    (repo / "keep.txt").unlink()

    unrestorable = restore_denied_paths(repo, ["keep.txt"])

    assert unrestorable == []
    assert (repo / "keep.txt").read_text() == "content\n"


def test_restore_glob_named_file_leaves_other_tracked_files_alone(tmp_path: Path):
    # Pathspec injection regression: a worker can create a file literally
    # named "*.env". Restoring it must not expand into a glob checkout that
    # reverts the operator's own dirty tracked files.
    repo = _git_repo(tmp_path, {"app.env": "original\n"})
    (repo / "app.env").write_text("operator WIP\n")  # dirty tracked file
    (repo / "*.env").write_text("SECRET=1\n")  # run-created, literal name

    unrestorable = restore_denied_paths(repo, ["*.env"], created=["*.env"])

    assert unrestorable == []
    assert not (repo / "*.env").exists()  # the literal file was unlinked
    assert (repo / "app.env").read_text() == "operator WIP\n"  # WIP untouched


def test_restore_tracked_glob_named_file_reverts_only_itself(tmp_path: Path):
    repo = _git_repo(
        tmp_path, {"*.env": "literal original\n", "app.env": "app original\n"}
    )
    (repo / "*.env").write_text("tampered\n")
    (repo / "app.env").write_text("operator WIP\n")

    unrestorable = restore_denied_paths(repo, ["*.env"])

    assert unrestorable == []
    assert (repo / "*.env").read_text() == "literal original\n"
    assert (repo / "app.env").read_text() == "operator WIP\n"


def test_restore_refuses_pathspec_magic_and_escaping_paths(tmp_path: Path):
    repo = _git_repo(tmp_path)
    hostile = [":/README.md", "/etc/passwd", "../escape.txt"]

    # Even when flagged as run-created, hostile shapes are refused — an
    # absolute path in `created` must never reach unlink.
    unrestorable = restore_denied_paths(repo, hostile, created=hostile)

    assert unrestorable == hostile
    assert (repo / "README.md").exists()


# --- flow integration: worker doubles -------------------------------------------


class WritingWorker:
    """Edit-mode double that writes files into whatever cwd it is given."""

    def __init__(self, writes: dict[str, str], success: bool = True):
        self.writes = writes
        self.success = success
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        cwd = Path(kwargs["cwd"])
        for rel_path, content in self.writes.items():
            target = cwd / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        if self.success:
            return CoderResult(summary="did work", raw_output="did work", exit_code=0)
        return CoderResult(
            summary="", raw_output="", exit_code=1, error="worker failed"
        )


class TaskWritingWorker:
    """Structured double writing per-task files (parallel batches)."""

    def __init__(self, task_writes: dict[int, dict[str, str]]):
        self.task_writes = task_writes

    def run(self, **kwargs):
        prompt = kwargs["task"]
        task_id = None
        for line in prompt.splitlines():
            if line.startswith("- Id: "):
                task_id = int(line.removeprefix("- Id: ").strip())
                break
        assert task_id is not None, "prompt lacked a task id"
        cwd = Path(kwargs["cwd"])
        for rel_path, content in self.task_writes[task_id].items():
            target = cwd / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return CoderResult(
            summary=f"task {task_id} complete",
            raw_output=f"task {task_id} complete",
            exit_code=0,
        )


def _do_work_config(deny: list[str], **do_work_extra) -> FlowConfig:
    return FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"do_work": {"worker": "claude", **do_work_extra}},
        defaults={"worker": "codex", "timeout": 300},
        paths={"deny": deny},
    )


def _structured_flow(
    repo: Path, cfg: FlowConfig, files: list[str]
) -> CrewAIHeadlessFlow:
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=files)
    ]
    return flow


# --- boundary 1: serial in-place ---------------------------------------------------


def test_serial_in_place_deny_restores_and_fails_task(tmp_path: Path):
    repo = _git_repo(tmp_path, {"src/a.py": "original\n"})
    flow = _structured_flow(repo, _do_work_config(["*.env"]), ["src/a.py"])
    flow._workers["do_work"] = WritingWorker(  # type: ignore
        {"src/a.py": "changed\n", "leak.env": "SECRET=1\n"}
    )

    cast(Any, flow).do_work("plan output")

    task = flow.state.tasks[0]
    assert task.status == "failed"
    assert "Denied paths touched" in (task.last_error or "")
    assert "leak.env" in (task.last_error or "")
    assert not (repo / "leak.env").exists()  # created file was unlinked
    # The allowed edit survives (partial edits are the in-place design);
    # containment for it is review/revise, not rollback.
    assert (repo / "src/a.py").read_text() == "changed\n"


def test_serial_in_place_deny_reports_unrestorable_preexisting_untracked(
    tmp_path: Path,
):
    repo = _git_repo(tmp_path, {"src/a.py": "original\n"})
    (repo / "operator.env").write_text("operator content\n")  # untracked dirt
    flow = _structured_flow(repo, _do_work_config(["*.env"]), ["src/a.py"])
    flow._workers["do_work"] = WritingWorker(  # type: ignore
        {"operator.env": "worker-tampered\n"}
    )

    cast(Any, flow).do_work("plan output")

    task = flow.state.tasks[0]
    assert task.status == "failed"
    assert "Could not restore" in (task.last_error or "")
    # Never delete operator data we cannot restore.
    assert (repo / "operator.env").read_text() == "worker-tampered\n"


# --- boundary 2: serial isolation: copy --------------------------------------------


def test_isolation_copy_merges_clean_result_into_target(tmp_path: Path):
    repo = _git_repo(tmp_path, {"src/a.py": "original\n"})
    flow = _structured_flow(repo, _do_work_config([], isolation="copy"), ["src/a.py"])
    worker = WritingWorker({"src/a.py": "improved\n"})
    flow._workers["do_work"] = worker  # type: ignore

    cast(Any, flow).do_work("plan output")

    assert flow.state.tasks[0].status == "done"
    assert (repo / "src/a.py").read_text() == "improved\n"
    assert worker.calls[0]["cwd"] != str(repo)  # ran in a copy
    assert flow.state.task_executions[0].isolated_workspace is True


def test_isolation_copy_failure_leaves_target_pristine(tmp_path: Path):
    repo = _git_repo(tmp_path, {"src/a.py": "original\n"})
    flow = _structured_flow(repo, _do_work_config([], isolation="copy"), ["src/a.py"])
    flow._workers["do_work"] = WritingWorker(  # type: ignore
        {"src/a.py": "half-broken\n", "junk.tmp": "x\n"}, success=False
    )

    cast(Any, flow).do_work("plan output")

    assert flow.state.tasks[0].status == "failed"
    assert (repo / "src/a.py").read_text() == "original\n"
    assert not (repo / "junk.tmp").exists()


def test_isolation_copy_deny_keeps_everything_out_of_target(tmp_path: Path):
    repo = _git_repo(tmp_path, {"src/a.py": "original\n"})
    flow = _structured_flow(
        repo, _do_work_config(["*.env"], isolation="copy"), ["src/a.py"]
    )
    flow._workers["do_work"] = WritingWorker(  # type: ignore
        {"src/a.py": "changed\n", "leak.env": "SECRET=1\n"}
    )

    cast(Any, flow).do_work("plan output")

    task = flow.state.tasks[0]
    assert task.status == "failed"
    assert "Denied paths touched" in (task.last_error or "")
    # Fail closed: nothing merged, not even the allowed file.
    assert (repo / "src/a.py").read_text() == "original\n"
    assert not (repo / "leak.env").exists()


def test_isolation_copy_creation_failure_fails_task_not_run(
    tmp_path: Path, monkeypatch
):
    repo = _git_repo(tmp_path, {"src/a.py": "original\n"})
    flow = _structured_flow(repo, _do_work_config([], isolation="copy"), ["src/a.py"])
    flow._workers["do_work"] = WritingWorker({"src/a.py": "changed\n"})  # type: ignore

    def exploding_copy(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        "crewai_headless_flow.stages.do_work.create_workspace_copy", exploding_copy
    )

    cast(Any, flow).do_work("plan output")

    task = flow.state.tasks[0]
    assert task.status == "failed"
    assert "Could not create isolated workspace copy" in (task.last_error or "")
    assert (repo / "src/a.py").read_text() == "original\n"


def test_unstructured_isolation_copy_creation_failure_records_error(
    tmp_path: Path, monkeypatch
):
    repo = _git_repo(tmp_path, {"src/a.py": "original\n"})
    flow = CrewAIHeadlessFlow(config=_do_work_config([], isolation="copy"))
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow._workers["do_work"] = WritingWorker({"src/a.py": "changed\n"})  # type: ignore

    def exploding_copy(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        "crewai_headless_flow.stages.do_work.create_workspace_copy", exploding_copy
    )

    cast(Any, flow).do_work("plan output")

    assert (repo / "src/a.py").read_text() == "original\n"
    assert any(
        "Could not create isolated workspace copy" in err for err in flow.state.errors
    )


# --- boundary 3: parallel mergeback -------------------------------------------------


def test_parallel_deny_fails_task_and_merges_nothing_from_it(tmp_path: Path):
    repo = _git_repo(tmp_path, {"src/a.py": "before a\n", "src/b.py": "before b\n"})
    cfg = _do_work_config(["*.env"], parallel={"enabled": True, "max_workers": 2})
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"]),
        TaskItem(id=2, title="two", description="task two", files=["src/b.py"]),
    ]
    flow._workers["do_work"] = TaskWritingWorker(  # type: ignore
        {
            1: {"src/a.py": "after a\n"},
            2: {"src/b.py": "after b\n", "leak.env": "SECRET=1\n"},
        }
    )

    cast(Any, flow).do_work("plan output")

    task_one, task_two = flow.state.tasks
    assert task_one.status == "done"
    assert task_two.status == "failed"
    assert "Denied paths touched" in (task_two.last_error or "")
    assert (repo / "src/a.py").read_text() == "after a\n"
    # Fail closed: nothing from the denied task left its workspace copy.
    assert (repo / "src/b.py").read_text() == "before b\n"
    assert not (repo / "leak.env").exists()


# --- boundary 4: unstructured (task-less) edit --------------------------------------


def test_unstructured_in_place_deny_restores_and_records_error(tmp_path: Path):
    repo = _git_repo(tmp_path, {"src/a.py": "original\n"})
    flow = CrewAIHeadlessFlow(config=_do_work_config(["*.env"]))
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow._workers["do_work"] = WritingWorker({"leak.env": "SECRET=1\n"})  # type: ignore

    summary = cast(Any, flow).do_work("plan output")

    assert not (repo / "leak.env").exists()
    assert any("Denied paths touched" in err for err in flow.state.errors)
    assert "Denied paths touched" in summary
    assert "leak.env" not in flow.state.changed_files


def test_unstructured_isolation_copy_deny_leaves_target_pristine(tmp_path: Path):
    repo = _git_repo(tmp_path, {"src/a.py": "original\n"})
    flow = CrewAIHeadlessFlow(config=_do_work_config(["*.env"], isolation="copy"))
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    worker = WritingWorker({"src/a.py": "changed\n", "leak.env": "SECRET=1\n"})
    flow._workers["do_work"] = worker  # type: ignore

    cast(Any, flow).do_work("plan output")

    assert worker.calls[0]["cwd"] != str(repo)
    assert (repo / "src/a.py").read_text() == "original\n"
    assert not (repo / "leak.env").exists()
    assert any("Denied paths touched" in err for err in flow.state.errors)


def test_unstructured_edit_detects_changes_via_snapshot_diff(tmp_path: Path):
    # The unstructured path no longer trusts the worker's self-report.
    repo = _git_repo(tmp_path, {"src/a.py": "original\n"})
    flow = CrewAIHeadlessFlow(config=_do_work_config([]))
    flow._state = FlowState(request="test", target_repo=str(repo))  # type: ignore[attr-defined]
    flow._workers["do_work"] = WritingWorker({"src/a.py": "changed\n"})  # type: ignore

    cast(Any, flow).do_work("plan output")

    assert "src/a.py" in flow.state.changed_files


# --- finalize boundary + delivery filter ---------------------------------------------


class LeakyFinalizeWorker:
    """Finalize double that writes the ADR plus a denied file."""

    def run(self, **kwargs) -> CoderResult:
        cwd = Path(kwargs["cwd"])
        (cwd / "docs").mkdir(exist_ok=True)
        (cwd / "docs/ADR.md").write_text("# ADR\n")
        (cwd / "secrets").mkdir(exist_ok=True)
        (cwd / "secrets/key.pem").write_text("PRIVATE\n")
        return CoderResult(
            summary="wrote ADR", changed_files=[], raw_output="wrote ADR", exit_code=0
        )


def test_finalize_deny_restores_and_excludes_from_delivery(tmp_path: Path):
    repo = _git_repo(tmp_path, {"src/feature.py": "feature\n"})
    cfg = FlowConfig(
        skills={"finalize": "documentation-and-adrs"},
        workers={"finalize": {"worker": "claude"}},
        defaults={"worker": "codex", "timeout": 300},
        deliver={"enabled": True},
        paths={"deny": ["secrets/*"]},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="ship it",
        target_repo=str(repo),
        run_id="20260710-153000-ship-it-3fa2b1cd",
        review_status="pass",
    )
    flow._workers["finalize"] = LeakyFinalizeWorker()  # type: ignore

    cast(Any, flow).finalize("pass")

    assert flow.state.status == "completed"
    assert not (repo / "secrets/key.pem").exists()  # restored (unlinked)
    assert any("Finalize:" in err for err in flow.state.errors)
    report = flow.state.delivery_report
    assert report is not None
    assert report.status == "committed"
    assert "docs/ADR.md" in report.staged_files
    assert not any("secrets" in path for path in report.staged_files)


def test_delivery_filter_excludes_denied_paths_tracked_earlier(tmp_path: Path):
    repo = _git_repo(tmp_path, {"src/feature.py": "feature\n"})
    cfg = FlowConfig(
        skills={"finalize": "documentation-and-adrs"},
        workers={"finalize": {"worker": "claude"}},
        defaults={"worker": "codex", "timeout": 300},
        deliver={"enabled": True},
        paths={"deny": ["*.env"]},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="ship it",
        target_repo=str(repo),
        run_id="20260710-153000-ship-it-3fa2b1cd",
        review_status="pass",
        # e.g. an unrestorable denied path a failed task still tracked
        changed_files=["src/feature.py", "old.env"],
    )

    class QuietFinalizeWorker:
        def run(self, **kwargs) -> CoderResult:
            return CoderResult(
                summary="done", changed_files=[], raw_output="done", exit_code=0
            )

    flow._workers["finalize"] = QuietFinalizeWorker()  # type: ignore
    (repo / "src/feature.py").write_text("feature v2\n")

    cast(Any, flow).finalize("pass")

    report = flow.state.delivery_report
    assert report is not None
    assert "old.env" not in report.staged_files
    assert any("Delivery excluded denied paths" in err for err in flow.state.errors)
