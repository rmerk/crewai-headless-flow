"""
Gap 3 (autonomy Phase 1): RunStore — run identity and durable, atomic
per-run artifacts (runs/<run_id>/state.json + debug_report.md).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest

from crewai_headless_flow.config import FlowConfig
from crewai_headless_flow.flow import CrewAIHeadlessFlow
from tests.flow_test_helpers import patch_build_headless_flow
from crewai_headless_flow.run_store import (
    RunStore,
    generate_run_id,
    slugify_request,
)
from crewai_headless_flow.state import FlowState, TaskItem
from crewai_headless_flow.workers.base import CoderResult


pytestmark = pytest.mark.offline


FIXED_NOW = datetime(2026, 7, 10, 15, 30, 0)


def test_generate_run_id_is_sortable_and_scannable():
    run_id = generate_run_id(
        "Add a resume feature!", now=FIXED_NOW, uuid_hex="3fa2b1cd99"
    )

    assert run_id == "20260710-153000-add-a-resume-feature-3fa2b1cd"


def test_generate_run_id_unique_without_injected_uuid():
    ids = {generate_run_id("same request", now=FIXED_NOW) for _ in range(20)}

    assert len(ids) == 20


def test_slugify_request_edge_cases():
    assert slugify_request("") == "run"
    assert slugify_request("///???") == "run"
    assert slugify_request("Fix the CSV Export, please") == "fix-the-csv-export-pleas"
    assert len(slugify_request("x" * 100)) <= 24
    assert not slugify_request("add feature ").endswith("-")


def test_allocate_creates_run_dir(tmp_path: Path):
    store = RunStore.allocate(tmp_path, "add feature", now=FIXED_NOW)

    assert store.run_dir.is_dir()
    assert store.run_dir.parent == tmp_path
    assert store.run_id == store.run_dir.name


def test_attach_requires_existing_dir(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        RunStore.attach(tmp_path / "missing")

    (tmp_path / "existing").mkdir()
    store = RunStore.attach(tmp_path / "existing")
    assert store.run_id == "existing"


def test_save_state_is_atomic_and_leaves_no_temp_files(tmp_path: Path):
    store = RunStore.allocate(tmp_path, "add feature", now=FIXED_NOW)

    store.save_state(json.dumps({"status": "running"}))
    store.save_state(json.dumps({"status": "completed"}))

    assert json.loads(store.state_path.read_text()) == {"status": "completed"}
    leftovers = [p for p in store.run_dir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_save_debug_report(tmp_path: Path):
    store = RunStore.allocate(tmp_path, "add feature", now=FIXED_NOW)

    store.save_debug_report("# Report\n")

    assert store.debug_report_path.read_text() == "# Report\n"


class CheckpointStubWorker:
    def run(self, **kwargs) -> CoderResult:
        if kwargs.get("mode") == "inspect":
            return CoderResult(
                summary="review complete",
                raw_output='{"status": "pass", "issues": [], "summary": "ok"}',
                exit_code=0,
            )
        return CoderResult(summary="work done", raw_output="work done", exit_code=0)


def test_flow_checkpoints_state_at_every_refresh(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = RunStore.allocate(tmp_path / "runs", "checkpoint test", now=FIXED_NOW)
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"do_work": {"worker": "claude"}},
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg, run_store=store)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="checkpoint test",
        target_repo=str(repo),
        run_id=store.run_id,
        run_dir=str(store.run_dir),
    )
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"])
    ]
    flow._workers["do_work"] = CheckpointStubWorker()  # type: ignore

    cast(Any, flow).do_work("plan output")

    saved = json.loads(store.state_path.read_text())
    assert saved["run_id"] == store.run_id
    assert saved["last_stage"] == "do_work"
    assert saved["tasks"][0]["status"] == "done"
    assert store.debug_report_path.exists()


def test_checkpoint_write_failure_never_kills_the_run(
    tmp_path: Path, caplog, monkeypatch: pytest.MonkeyPatch
):
    # A prior CLI test may have configured the package logger with
    # propagate=False; caplog listens on the root logger.
    monkeypatch.setattr(logging.getLogger("crewai_headless_flow"), "propagate", True)
    store = RunStore.allocate(tmp_path / "runs", "doomed writes", now=FIXED_NOW)
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"do_work": {"worker": "claude"}},
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg, run_store=store)
    flow._state = FlowState(request="doomed writes", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    # Simulate a dead disk / removed run dir.
    def boom(state_json: str) -> None:
        raise OSError("disk gone")

    store.save_state = boom  # type: ignore[method-assign]

    with caplog.at_level("WARNING", logger="crewai_headless_flow.flow"):
        flow._refresh_debug_report()

    assert "checkpoint write failed" in caplog.text


def test_run_headless_flow_stamps_run_identity_before_kickoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from crewai_headless_flow import flow as flow_module

    captured: dict = {}

    class FakeFlow:
        def __init__(self, config=None, run_store=None):
            self.config = config
            self.run_store = run_store
            self._state = FlowState()

        @property
        def state(self):
            return self._state

        def kickoff(self, inputs):
            captured["inputs"] = inputs
            captured["run_store"] = self.run_store
            self._state = FlowState.model_validate(inputs)

    patch_build_headless_flow(monkeypatch, flow_module, FakeFlow)

    result = flow_module.run_headless_flow(
        request="stamp identity",
        target_repo=str(tmp_path / "repo"),
        runs_dir=tmp_path / "runs",
    )

    run_id = captured["inputs"]["run_id"]
    assert run_id is not None
    assert "stamp-identity" in run_id
    assert captured["inputs"]["run_dir"] == str(tmp_path / "runs" / run_id)
    assert captured["inputs"]["created_at"] is not None
    assert captured["run_store"].run_id == run_id
    assert (tmp_path / "runs" / run_id).is_dir()
    # Identity rides through kickoff's state rehydration.
    assert result.run_id == run_id


def test_run_headless_flow_without_runs_dir_has_no_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from crewai_headless_flow import flow as flow_module

    class FakeFlow:
        def __init__(self, config=None, run_store=None):
            self.run_store = run_store
            self._state = FlowState()

        @property
        def state(self):
            return self._state

        def kickoff(self, inputs):
            self._state = FlowState.model_validate(inputs)
            assert self.run_store is None

    patch_build_headless_flow(monkeypatch, flow_module, FakeFlow)

    result = flow_module.run_headless_flow(
        request="no identity",
        target_repo=str(tmp_path / "repo"),
    )

    assert result.run_id is None
    assert result.run_dir is None


# =============================================================================
# JSONL event log (autonomy Gap 9a)
# =============================================================================


def test_append_event_writes_parseable_lines(tmp_path: Path):
    store = RunStore.allocate(tmp_path, "add feature", now=FIXED_NOW)

    store.append_event(json.dumps({"kind": "stage_start", "stage": "plan"}))
    store.append_event(json.dumps({"kind": "run_completed"}))

    lines = store.events_path.read_text().splitlines()
    assert [json.loads(line)["kind"] for line in lines] == [
        "stage_start",
        "run_completed",
    ]


def test_events_file_is_created_lazily(tmp_path: Path):
    store = RunStore.allocate(tmp_path, "add feature", now=FIXED_NOW)

    assert not store.events_path.exists()
    store.append_event("{}")
    assert store.events_path.exists()


def test_append_event_continues_same_file_after_attach(tmp_path: Path):
    store = RunStore.allocate(tmp_path, "add feature", now=FIXED_NOW)
    store.append_event(json.dumps({"kind": "stage_start"}))

    resumed = RunStore.attach(store.run_dir)
    resumed.append_event(json.dumps({"kind": "run_completed"}))

    lines = store.events_path.read_text().splitlines()
    assert len(lines) == 2


def test_flow_emits_ordered_events_with_deterministic_timestamps(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = RunStore.allocate(tmp_path / "runs", "event test", now=FIXED_NOW)
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"do_work": {"worker": "claude"}},
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg, run_store=store)
    flow._now_fn = lambda: FIXED_NOW
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="event test",
        target_repo=str(repo),
        run_id=store.run_id,
        run_dir=str(store.run_dir),
    )
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"])
    ]
    flow._workers["do_work"] = CheckpointStubWorker()  # type: ignore

    cast(Any, flow).do_work("plan output")

    events = [json.loads(line) for line in store.events_path.read_text().splitlines()]
    kinds = [event["kind"] for event in events]
    assert kinds[0] == "stage_start"
    assert "task_complete" in kinds
    for event in events:
        assert event["ts"] == "2026-07-10T15:30:00"
        assert event["run_id"] == store.run_id
        assert "revision" in event


def test_flow_without_run_store_emits_no_events_and_never_fails(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"do_work": {"worker": "claude"}},
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="no store", target_repo=str(repo))  # type: ignore[attr-defined]
    flow._workers["do_work"] = CheckpointStubWorker()  # type: ignore

    cast(Any, flow).do_work("plan output")

    assert flow.state.status == "running"
    assert not list(tmp_path.glob("**/events.jsonl"))


def test_event_write_failure_never_kills_the_run(
    tmp_path: Path, caplog, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(logging.getLogger("crewai_headless_flow"), "propagate", True)
    store = RunStore.allocate(tmp_path / "runs", "doomed events", now=FIXED_NOW)
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"do_work": {"worker": "claude"}},
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg, run_store=store)
    flow._state = FlowState(request="doomed events", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    def boom(line: str) -> None:
        raise OSError("disk gone")

    store.append_event = boom  # type: ignore[method-assign]

    with caplog.at_level("WARNING", logger="crewai_headless_flow.flow"):
        flow._log_event("stage_start", stage="plan")

    assert "event write failed" in caplog.text


def test_resumed_flow_appends_to_same_events_file(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = RunStore.allocate(tmp_path / "runs", "resume events", now=FIXED_NOW)
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"do_work": {"worker": "claude"}},
        defaults={"worker": "codex", "timeout": 300},
    )

    def make_flow() -> CrewAIHeadlessFlow:
        flow = CrewAIHeadlessFlow(config=cfg, run_store=RunStore.attach(store.run_dir))
        flow._now_fn = lambda: FIXED_NOW
        flow._state = FlowState(  # type: ignore[attr-defined]
            request="resume events",
            target_repo=str(repo),
            run_id=store.run_id,
            run_dir=str(store.run_dir),
        )
        flow._workers["do_work"] = CheckpointStubWorker()  # type: ignore
        return flow

    cast(Any, make_flow()).do_work("first process")
    cast(Any, make_flow()).do_work("second process")  # simulates a resume

    events = [json.loads(line) for line in store.events_path.read_text().splitlines()]
    stage_starts = [e for e in events if e["kind"] == "stage_start"]
    assert len(stage_starts) == 2


# =============================================================================
# summarize_runs — run-history listing (autonomy Phase 3)
# =============================================================================


def _write_run(base: Path, run_id: str, state: dict | None) -> Path:
    run_dir = base / run_id
    run_dir.mkdir(parents=True)
    if state is not None:
        (run_dir / "state.json").write_text(json.dumps(state))
    return run_dir


def test_summarize_runs_lists_newest_first_with_state_fields(tmp_path: Path):
    from crewai_headless_flow.run_store import summarize_runs

    _write_run(
        tmp_path,
        "20260710-100000-older-run-aa",
        {
            "status": "completed",
            "request": "add   auth support",
            "revisions": 1,
            "max_revisions": 2,
            "tasks": [{"status": "done"}, {"status": "failed"}],
            "delivery_report": {"branch": "flow/older"},
            "changed_files": ["a.py", "b.py"],
        },
    )
    _write_run(
        tmp_path,
        "20260711-120000-newer-run-bb",
        {"status": "failed", "request": "fix bug", "revisions": 2, "max_revisions": 2},
    )

    summaries = summarize_runs(tmp_path)

    assert [entry["run_id"] for entry in summaries] == [
        "20260711-120000-newer-run-bb",
        "20260710-100000-older-run-aa",
    ]
    older = summaries[1]
    assert older["status"] == "completed"
    assert older["request"] == "add auth support"  # whitespace collapsed
    assert older["revisions"] == 1
    assert older["tasks_done"] == 1
    assert older["tasks_total"] == 2
    assert older["branch"] == "flow/older"
    assert older["changed_files"] == 2


def test_summarize_runs_reports_unreadable_state_as_unknown(tmp_path: Path):
    from crewai_headless_flow.run_store import summarize_runs

    _write_run(tmp_path, "20260711-130000-no-state-cc", None)
    broken = _write_run(tmp_path, "20260711-140000-broken-dd", None)
    (broken / "state.json").write_text("{not json")

    summaries = summarize_runs(tmp_path)

    assert [entry["status"] for entry in summaries] == ["unknown", "unknown"]
    assert summaries[0]["run_id"] == "20260711-140000-broken-dd"


def test_summarize_runs_honors_limit_and_missing_dir(tmp_path: Path):
    from crewai_headless_flow.run_store import summarize_runs

    for index in range(3):
        _write_run(
            tmp_path, f"2026071{index}-000000-r-{index}", {"status": "completed"}
        )

    assert len(summarize_runs(tmp_path, limit=2)) == 2
    assert summarize_runs(tmp_path / "does-not-exist") == []


def test_cli_runs_lists_history(tmp_path: Path, capsys):
    from crewai_headless_flow import cli

    # A pre-existing handler keeps _configure_logging from binding this
    # test's captured stdout, so --format json output stays parseable.
    logging.getLogger("crewai_headless_flow").addHandler(logging.NullHandler())

    _write_run(
        tmp_path,
        "20260711-120000-list-me-ee",
        {
            "status": "completed",
            "request": "list me",
            "revisions": 0,
            "max_revisions": 2,
            "delivery_report": {"branch": "flow/list-me"},
        },
    )

    exit_code = cli.main(["runs", "--runs-dir", str(tmp_path), "--format", "json"])

    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["run_id"] == "20260711-120000-list-me-ee"
    assert data[0]["branch"] == "flow/list-me"
