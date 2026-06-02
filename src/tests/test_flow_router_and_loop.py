"""
Milestone 5: Flow topology tests using a fully stubbed worker.

These tests exercise the @router, revise loop, and cap logic without
touching any real CLI or network.
"""

from __future__ import annotations

import json

import pytest

from crewai_headless_flow.config import FlowConfig
from crewai_headless_flow.flow import CrewAIHeadlessFlow
from crewai_headless_flow.review_crew import ReviewCrewDecision
from crewai_headless_flow.state import FlowState
from crewai_headless_flow.workers import ClaudeAdapter
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


def test_flow_builds_claude_worker_from_worker_config():
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"do_work": {"worker": "claude"}},
        defaults={"worker": "codex", "timeout": 300},
    )

    flow = CrewAIHeadlessFlow(config=cfg)

    assert isinstance(flow._get_worker("do_work").worker, ClaudeAdapter)


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

    flow.finalize("pass")

    assert worker.calls[0]["model"] == "sonnet"


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

    stub = StubWorker(review_outcome="revise", issues=["bad"])
    flow._workers["review"] = stub  # type: ignore

    flow.review("work")
    flow.revise("revise")

    # Now force another review — it should not allow more than max
    assert flow.state.revisions == 1

    # State should respect the cap
    assert flow.state.revisions <= flow.state.max_revisions


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
        "crewai_headless_flow.flow.run_review_crew",
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
        "crewai_headless_flow.flow.run_review_crew",
        fake_run_review_crew,
    )

    decision = flow.review("work summary")

    assert decision == "revise"
    assert flow.state.review_status == "revise"
    assert flow.state.issues == ["Review Crew failed: crew unavailable"]
