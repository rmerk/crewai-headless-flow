"""
Gap 3 (autonomy Phase 1): resume-from-crash.

A process that dies mid-run leaves status == "running" on its last
checkpoint. resume_headless_flow now accepts that state and synthesizes a
resume checkpoint from last_stage. Critically, last_stage is NOT "last
completed stage" — structured do_work and review set it before their worker
calls — so the mapping re-runs the named stage.
"""

from __future__ import annotations

import pytest

from crewai_headless_flow import flow as flow_module
from crewai_headless_flow.flow import synthesize_crash_checkpoint
from crewai_headless_flow.state import FlowState, TaskItem


pytestmark = pytest.mark.offline


def _make_fake_flow(calls: list[tuple[str, str | None]]):
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
            calls.append(("plan", None))
            return "plan output"

        def do_work(self, plan_output: str) -> str:
            calls.append(("do_work", plan_output))
            return "work summary"

        def review(self, work_summary: str) -> str:
            calls.append(("review", work_summary))
            return "pass"

        def revise(self, decision: str) -> str:
            calls.append(("revise", decision))
            return "revise prompt"

        def finalize(self, decision: str) -> str:
            calls.append(("finalize", decision))
            self.state.status = "completed"
            return "done"

    return FakeFlow


def _crashed_state(**kwargs) -> FlowState:
    defaults: dict[str, object] = dict(
        request="crashed run", target_repo="/tmp/fake", status="running"
    )
    defaults.update(kwargs)
    return FlowState(**defaults)  # type: ignore[arg-type]


def _tasks() -> list[TaskItem]:
    return [
        TaskItem(id=1, title="one", description="task one", status="done"),
        TaskItem(id=2, title="two", description="task two", status="in_progress"),
    ]


def test_crash_before_plan_replays_from_plan(monkeypatch):
    calls: list[tuple[str, str | None]] = []
    def _fake_build(*, config=None, run_store=None, config_dir=None):
        return _make_fake_flow(calls)(config=config, run_store=run_store)

    monkeypatch.setattr(flow_module, "build_headless_flow", _fake_build)
    state = _crashed_state(last_stage=None)

    resumed = flow_module.resume_headless_flow(state)

    assert [name for name, _ in calls] == ["plan", "do_work", "review", "finalize"]
    assert resumed.status == "completed"


def test_crash_after_plan_resumes_do_work_with_rebuilt_plan(monkeypatch):
    calls: list[tuple[str, str | None]] = []
    def _fake_build(*, config=None, run_store=None, config_dir=None):
        return _make_fake_flow(calls)(config=config, run_store=run_store)

    monkeypatch.setattr(flow_module, "build_headless_flow", _fake_build)
    state = _crashed_state(
        last_stage="plan",
        spec="Implement the feature.",
        tasks=[TaskItem(id=1, title="Add it", description="Add the feature.")],
    )

    resumed = flow_module.resume_headless_flow(state)

    assert [name for name, _ in calls] == ["do_work", "review", "finalize"]
    assert "<spec>" in (calls[0][1] or "")
    assert "1. Add it" in (calls[0][1] or "")
    assert resumed.status == "completed"


def test_crash_mid_do_work_reruns_do_work_and_resets_in_progress_tasks(monkeypatch):
    calls: list[tuple[str, str | None]] = []
    def _fake_build(*, config=None, run_store=None, config_dir=None):
        return _make_fake_flow(calls)(config=config, run_store=run_store)

    monkeypatch.setattr(flow_module, "build_headless_flow", _fake_build)
    state = _crashed_state(
        last_stage="do_work", spec="Implement the feature.", tasks=_tasks()
    )

    resumed = flow_module.resume_headless_flow(state)

    assert [name for name, _ in calls] == ["do_work", "review", "finalize"]
    assert resumed.tasks[0].status == "done"  # done tasks stay done
    assert resumed.tasks[1].status != "in_progress"  # interrupted work reopened
    assert resumed.status == "completed"


def test_crash_mid_review_reruns_review_from_saved_work_summary(monkeypatch):
    calls: list[tuple[str, str | None]] = []
    def _fake_build(*, config=None, run_store=None, config_dir=None):
        return _make_fake_flow(calls)(config=config, run_store=run_store)

    monkeypatch.setattr(flow_module, "build_headless_flow", _fake_build)
    state = _crashed_state(
        last_stage="review", latest_work_summary="the saved work summary"
    )

    resumed = flow_module.resume_headless_flow(state)

    assert calls == [
        ("review", "the saved work summary"),
        ("finalize", "pass"),
    ]
    assert resumed.status == "completed"


def test_crash_mid_review_without_summary_falls_back_to_do_work(monkeypatch):
    calls: list[tuple[str, str | None]] = []
    def _fake_build(*, config=None, run_store=None, config_dir=None):
        return _make_fake_flow(calls)(config=config, run_store=run_store)

    monkeypatch.setattr(flow_module, "build_headless_flow", _fake_build)
    state = _crashed_state(
        last_stage="review",
        latest_work_summary=None,
        spec="Implement the feature.",
        tasks=[TaskItem(id=1, title="Add it", description="Add the feature.")],
    )

    resumed = flow_module.resume_headless_flow(state)

    assert [name for name, _ in calls] == ["do_work", "review", "finalize"]
    assert resumed.status == "completed"


def test_crash_after_force_revise_before_finalize_continues_revise_loop(monkeypatch):
    calls: list[tuple[str, str | None]] = []
    def _fake_build(*, config=None, run_store=None, config_dir=None):
        return _make_fake_flow(calls)(config=config, run_store=run_store)

    monkeypatch.setattr(flow_module, "build_headless_flow", _fake_build)
    state = _crashed_state(
        last_stage="finalize",
        review_status="revise",
        issues=["Human requested revision before finalize."],
    )

    resumed = flow_module.resume_headless_flow(state)

    assert [name for name, _ in calls] == ["revise", "do_work", "review", "finalize"]
    assert resumed.status == "completed"


def test_crash_mid_finalize_reruns_finalize(monkeypatch):
    calls: list[tuple[str, str | None]] = []
    def _fake_build(*, config=None, run_store=None, config_dir=None):
        return _make_fake_flow(calls)(config=config, run_store=run_store)

    monkeypatch.setattr(flow_module, "build_headless_flow", _fake_build)
    state = _crashed_state(last_stage="finalize", review_status="pass")

    resumed = flow_module.resume_headless_flow(state)

    assert calls == [("finalize", "pass")]
    assert resumed.status == "completed"


def test_crash_with_unknown_last_stage_raises(monkeypatch):
    state = _crashed_state(last_stage="revise")

    with pytest.raises(ValueError, match="Cannot resume crashed run"):
        flow_module.resume_headless_flow(state)


def test_completed_run_is_not_resumable():
    state = FlowState(request="done", target_repo="/tmp/fake", status="completed")

    with pytest.raises(ValueError, match="aborted_by_human"):
        flow_module.resume_headless_flow(state)


def test_synthesize_crash_checkpoint_mapping_table():
    assert synthesize_crash_checkpoint(_crashed_state(last_stage=None)).stage == "plan"
    assert (
        synthesize_crash_checkpoint(_crashed_state(last_stage="plan")).stage
        == "do_work"
    )
    assert (
        synthesize_crash_checkpoint(_crashed_state(last_stage="do_work")).stage
        == "do_work"
    )
    review_cp = synthesize_crash_checkpoint(
        _crashed_state(last_stage="review", latest_work_summary="summary")
    )
    assert (review_cp.stage, review_cp.stage_input) == ("review", "summary")
    assert (
        synthesize_crash_checkpoint(_crashed_state(last_stage="review")).stage
        == "do_work"
    )
    finalize_cp = synthesize_crash_checkpoint(_crashed_state(last_stage="finalize"))
    assert (finalize_cp.stage, finalize_cp.stage_input) == ("finalize", "pass")


def test_resume_run_store_attaches_existing_run_dir(tmp_path):
    run_dir = tmp_path / "runs" / "20260710-153000-crashed-run-abcd1234"
    run_dir.mkdir(parents=True)
    state = _crashed_state(run_id=run_dir.name, run_dir=str(run_dir))

    store = flow_module._resolve_resume_run_store(state, None)

    assert store is not None
    assert store.run_dir == run_dir


def test_resume_run_store_recreates_dir_from_run_id(tmp_path):
    state = _crashed_state(run_id="20260710-153000-crashed-run-abcd1234")

    store = flow_module._resolve_resume_run_store(state, tmp_path / "runs")

    assert store is not None
    assert store.run_dir == tmp_path / "runs" / state.run_id
    assert store.run_dir.is_dir()
    assert state.run_dir == str(store.run_dir)


def test_resume_run_store_allocates_fresh_when_state_has_no_identity(tmp_path):
    state = _crashed_state()

    store = flow_module._resolve_resume_run_store(state, tmp_path / "runs")

    assert store is not None
    assert state.run_id == store.run_id
    assert "crashed-run" in store.run_id
    assert store.run_dir.is_dir()


def test_resume_run_store_none_without_identity_or_runs_dir():
    state = _crashed_state()

    assert flow_module._resolve_resume_run_store(state, None) is None
