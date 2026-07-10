"""
Gap 5 (autonomy Phase 1): worker infrastructure exceptions must never escape
the HeadlessCoderTool seam — they become failed CoderResults that the Flow's
ordinary failure/revise machinery handles. Retries and the optional fallback
worker fire only on those exceptions, never on non-zero exits.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from crewai_headless_flow.config import FlowConfig
from crewai_headless_flow.flow import CrewAIHeadlessFlow
from crewai_headless_flow.state import FlowState, TaskItem
from crewai_headless_flow.tools.coder_tool import (
    INFRASTRUCTURE_FAILURE_EXIT_CODE,
    HeadlessCoderTool,
)
from crewai_headless_flow.workers.base import (
    CoderResult,
    WorkerInvocationError,
    WorkerTimeout,
)


pytestmark = pytest.mark.offline


class RaisingWorker:
    """Raises a configured infrastructure exception for the first N calls."""

    def __init__(self, exc: Exception, fail_times: int | None = None):
        self.exc = exc
        self.fail_times = fail_times
        self.calls: list[dict] = []

    def run(self, task: str, **kwargs) -> CoderResult:
        self.calls.append({"task": task, **kwargs})
        if self.fail_times is None or len(self.calls) <= self.fail_times:
            raise self.exc
        return CoderResult(summary="recovered", raw_output="recovered", exit_code=0)


class ReturningWorker:
    def __init__(self, result: CoderResult):
        self.result = result
        self.calls: list[dict] = []

    def run(self, task: str, **kwargs) -> CoderResult:
        self.calls.append({"task": task, **kwargs})
        return self.result


def test_tool_converts_worker_timeout_to_failed_result():
    worker = RaisingWorker(WorkerTimeout("Codex timed out after 300s"))
    tool = HeadlessCoderTool(worker=worker)  # type: ignore[arg-type]

    result = tool.run("do something", cwd="/tmp/repo")

    assert result.success is False
    assert result.exit_code == INFRASTRUCTURE_FAILURE_EXIT_CODE
    assert result.error == "WorkerTimeout: Codex timed out after 300s"
    assert len(worker.calls) == 1


def test_tool_converts_invocation_error_to_failed_result():
    worker = RaisingWorker(WorkerInvocationError("binary not found"))
    tool = HeadlessCoderTool(worker=worker)  # type: ignore[arg-type]

    result = tool.run("do something", cwd="/tmp/repo")

    assert result.success is False
    assert result.error == "WorkerInvocationError: binary not found"


def test_tool_retries_only_on_infrastructure_error_and_backs_off():
    worker = RaisingWorker(WorkerTimeout("flaky"), fail_times=1)
    sleeps: list[float] = []
    tool = HeadlessCoderTool(  # type: ignore[arg-type]
        worker=worker,
        max_attempts=2,
        backoff_seconds=5.0,
        sleep_fn=sleeps.append,
    )

    result = tool.run("do something", cwd="/tmp/repo")

    assert result.success is True
    assert result.summary == "recovered"
    assert len(worker.calls) == 2
    assert sleeps == [5.0]


def test_tool_does_not_retry_nonzero_exit():
    worker = ReturningWorker(
        CoderResult(summary="", raw_output="boom", exit_code=1, error="boom")
    )
    tool = HeadlessCoderTool(worker=worker, max_attempts=3)  # type: ignore[arg-type]

    result = tool.run("do something", cwd="/tmp/repo")

    assert result.success is False
    assert result.exit_code == 1
    assert len(worker.calls) == 1


def test_tool_fails_over_to_fallback_after_primary_exhausted():
    primary = RaisingWorker(WorkerTimeout("primary down"))
    fallback = ReturningWorker(
        CoderResult(summary="fallback did it", raw_output="ok", exit_code=0)
    )
    tool = HeadlessCoderTool(  # type: ignore[arg-type]
        worker=primary,
        fallback_worker=fallback,
        max_attempts=2,
        sleep_fn=lambda _: None,
    )

    result = tool.run("do something", cwd="/tmp/repo")

    assert result.success is True
    assert result.summary == "fallback did it"
    assert len(primary.calls) == 2
    assert len(fallback.calls) == 1


def test_tool_reports_last_error_when_fallback_also_raises():
    primary = RaisingWorker(WorkerTimeout("primary down"))
    fallback = RaisingWorker(WorkerInvocationError("fallback down"))
    tool = HeadlessCoderTool(  # type: ignore[arg-type]
        worker=primary,
        fallback_worker=fallback,
        sleep_fn=lambda _: None,
    )

    result = tool.run("do something", cwd="/tmp/repo")

    assert result.success is False
    assert result.exit_code == INFRASTRUCTURE_FAILURE_EXIT_CODE
    assert result.error == "WorkerInvocationError: fallback down"
    assert len(primary.calls) == 1
    assert len(fallback.calls) == 1


def test_tool_applies_same_augmented_task_to_fallback():
    primary = RaisingWorker(WorkerTimeout("primary down"))
    fallback = ReturningWorker(CoderResult(summary="ok", raw_output="ok", exit_code=0))
    tool = HeadlessCoderTool(  # type: ignore[arg-type]
        worker=primary,
        skill_name="incremental-implementation",
        fallback_worker=fallback,
    )

    tool.run("add the feature", cwd="/tmp/repo")

    assert primary.calls[0]["task"] == fallback.calls[0]["task"]
    assert "add the feature" in fallback.calls[0]["task"]
    assert "operating procedure" in fallback.calls[0]["task"]


def _stage_cfg(extra: dict) -> FlowConfig:
    return FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"do_work": {"worker": "claude", **extra}},
        defaults={"worker": "codex", "timeout": 300},
    )


def test_setup_workers_wires_retry_and_fallback_from_stage_extra():
    cfg = _stage_cfg(
        {
            "retry": {"max_attempts": 3, "backoff_seconds": 2},
            "fallback_worker": "gemini",
        }
    )

    flow = CrewAIHeadlessFlow(config=cfg)
    tool = flow._get_worker("do_work")

    assert tool.max_attempts == 3
    assert tool.backoff_seconds == 2.0
    assert tool.fallback_worker is not None
    assert type(tool.fallback_worker).__name__ == "GeminiAdapter"


def test_setup_workers_rejects_unknown_fallback_worker():
    cfg = _stage_cfg({"fallback_worker": "claude-typo"})

    with pytest.raises(
        ValueError,
        match="Unsupported fallback worker 'claude-typo' configured for stage 'do_work'",
    ):
        CrewAIHeadlessFlow(config=cfg)


def test_setup_workers_rejects_fallback_same_as_primary():
    cfg = _stage_cfg({"fallback_worker": "claude"})

    with pytest.raises(
        ValueError,
        match="fallback_worker for stage 'do_work' must differ",
    ):
        CrewAIHeadlessFlow(config=cfg)


def test_flow_routes_contained_timeout_into_task_failure():
    """A WorkerTimeout during do_work becomes an ordinary task failure —
    no exception escapes the Flow."""
    cfg = _stage_cfg({})
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="one", description="task one", files=["src/a.py"])
    ]
    raising = RaisingWorker(WorkerTimeout("Claude timed out after 300s"))
    flow._workers["do_work"] = HeadlessCoderTool(worker=raising)  # type: ignore

    cast(Any, flow).do_work("plan output")

    assert flow.state.tasks[0].status == "failed"
    assert "WorkerTimeout" in (flow.state.tasks[0].last_error or "")


def test_flow_review_survives_contained_worker_exception():
    """A review-stage infrastructure failure fails closed to 'revise' instead
    of crashing the run."""
    cfg = FlowConfig(
        skills={"review": "code-review-and-quality"},
        workers={"review": {"worker": "claude"}},
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    raising = RaisingWorker(WorkerTimeout("review hung"))
    flow._workers["review"] = HeadlessCoderTool(worker=raising)  # type: ignore

    decision = cast(Any, flow).review("work summary")

    assert decision == "revise"
    assert flow.state.review_status == "revise"
