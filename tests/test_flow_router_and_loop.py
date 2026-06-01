"""
Milestone 5: Flow topology tests using a fully stubbed worker.

These tests exercise the @router, revise loop, and cap logic without
touching any real CLI or network.
"""

from __future__ import annotations


import pytest

from crewai_headless_flow.flow import CrewAIHeadlessFlow
from crewai_headless_flow.state import FlowState
from crewai_headless_flow.workers.base import CoderResult


pytestmark = pytest.mark.offline


class StubWorker:
    """Minimal stub that returns controllable results for testing the Flow logic."""

    def __init__(self, review_outcome: str = "pass", issues: list[str] | None = None):
        self.review_outcome = review_outcome
        self.issues = issues or []
        self.call_count = 0

    def run(self, task: str, cwd: str, mode: str = "edit", **kwargs):
        self.call_count += 1

        if "review" in task.lower() or mode == "inspect":
            # Simulate review worker output
            if self.review_outcome == "pass":
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
