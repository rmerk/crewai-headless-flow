"""Offline unit tests for the Conditional HITL policy (``hitl_policy``).

Covers: static-mode passthrough, disabled-trigger silence, the ``min_attempts``
off-by-one boundary, consecutive-failure-since-last-success semantics,
revision-spanning streaks, the multi-task scan + worst-offender tie-break, and
the ``approaching_max_revisions`` boundary.
"""

from __future__ import annotations

from typing import Literal

import pytest

from crewai_headless_flow.hitl_policy import (
    ApproachingMaxRevisionsDetail,
    GateContext,
    RepeatedTaskFailureDetail,
    should_prompt,
)
from crewai_headless_flow.state import (
    FlowState,
    HumanFeedbackEntry,
    TaskExecutionEntry,
    TaskItem,
    TriggerReason,
)

pytestmark = pytest.mark.offline


def _entry(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "stage": "do_work",
        "gate": "before_do_work",
        "approved": True,
        "response": "y",
        "worker": "codex",
        "skill": "incremental-implementation",
        "message": "proceed?",
    }
    base.update(overrides)
    return base


_TaskStatus = Literal["pending", "in_progress", "needs_revision", "failed", "done"]


def _task(task_id: int, status: _TaskStatus = "pending") -> TaskItem:
    return TaskItem(id=task_id, description=f"task {task_id}", status=status)


def _exec(task_id: int, *, success: bool, revision: int = 0) -> TaskExecutionEntry:
    return TaskExecutionEntry(
        task_id=task_id,
        attempt=1,
        revision=revision,
        worker="codex",
        success=success,
        summary="",
    )


def _conditional(**triggers: dict[str, object]) -> dict[str, object]:
    return {"mode": "conditional", "conditional": {"triggers": triggers}}


# --- Static mode -------------------------------------------------------------


def test_static_mode_uses_gate_boolean_true() -> None:
    decision = should_prompt("before_do_work", {"before_do_work": True}, FlowState())
    assert decision.should_prompt is True
    assert decision.trigger_reason is None


def test_static_mode_uses_gate_boolean_false() -> None:
    decision = should_prompt("before_do_work", {"before_do_work": False}, FlowState())
    assert decision.should_prompt is False


def test_static_mode_missing_gate_defaults_true() -> None:
    # Matches the historical ``hf.get(gate, True)`` behavior.
    assert should_prompt("before_do_work", {}, FlowState()).should_prompt is True


def test_static_mode_ignores_conditional_triggers() -> None:
    hf = {
        "before_do_work": False,
        "conditional": {"triggers": {"repeated_task_failure": {"enabled": True}}},
    }
    # No ``mode: conditional`` → conditional.* is inert, boolean wins.
    assert should_prompt("before_do_work", hf, FlowState()).should_prompt is False


# --- Conditional mode: disabled / silent -------------------------------------


def test_conditional_disabled_trigger_never_fires() -> None:
    hf = _conditional(repeated_task_failure={"enabled": False, "min_attempts": 1})
    state = FlowState(task_executions=[_exec(1, success=False)])
    ctx = GateContext(tasks=(_task(1),))
    assert should_prompt("before_do_work", hf, state, ctx).should_prompt is False


def test_conditional_legacy_boolean_ignored_when_no_trigger() -> None:
    # before_review has no Phase 0 trigger → silent under conditional mode even
    # though its legacy boolean is True (the "silent gate" consequence).
    hf = {**_conditional(), "before_review": True}
    assert should_prompt("before_review", hf, FlowState()).should_prompt is False


# --- repeated_task_failure: off-by-one + semantics ---------------------------


def test_repeated_failure_off_by_one_fires_after_one_failure() -> None:
    hf = _conditional(repeated_task_failure={"enabled": True, "min_attempts": 2})
    state = FlowState(task_executions=[_exec(1, success=False)])
    ctx = GateContext(tasks=(_task(1),))
    decision = should_prompt("before_do_work", hf, state, ctx)
    assert decision.should_prompt is True
    assert decision.trigger_reason is not None
    assert decision.trigger_reason.kind == "repeated_task_failure"
    detail = decision.trigger_reason.detail
    assert isinstance(detail, RepeatedTaskFailureDetail)
    assert detail == RepeatedTaskFailureDetail(task_id=1, attempts=1)


def test_repeated_failure_does_not_fire_before_first_failure() -> None:
    hf = _conditional(repeated_task_failure={"enabled": True, "min_attempts": 2})
    state = FlowState(task_executions=[])
    ctx = GateContext(tasks=(_task(1),))
    assert should_prompt("before_do_work", hf, state, ctx).should_prompt is False


def test_repeated_failure_counts_only_trailing_failures_since_success() -> None:
    # Success, then 1 failure → streak is 1, not 3 total executions.
    hf = _conditional(repeated_task_failure={"enabled": True, "min_attempts": 3})
    state = FlowState(
        task_executions=[
            _exec(1, success=False),
            _exec(1, success=True),
            _exec(1, success=False),
        ]
    )
    ctx = GateContext(tasks=(_task(1, status="needs_revision"),))
    # min_attempts=3 needs streak >= 2; trailing streak is only 1 → no fire.
    assert should_prompt("before_do_work", hf, state, ctx).should_prompt is False


def test_repeated_failure_streak_spans_revisions() -> None:
    hf = _conditional(repeated_task_failure={"enabled": True, "min_attempts": 2})
    state = FlowState(
        task_executions=[
            _exec(1, success=False, revision=0),
            _exec(1, success=False, revision=1),
        ]
    )
    ctx = GateContext(tasks=(_task(1, status="needs_revision"),))
    decision = should_prompt("before_do_work", hf, state, ctx)
    assert decision.should_prompt is True
    assert decision.trigger_reason is not None
    assert decision.trigger_reason.detail == RepeatedTaskFailureDetail(
        task_id=1, attempts=2
    )


def test_repeated_failure_skips_done_tasks() -> None:
    hf = _conditional(repeated_task_failure={"enabled": True, "min_attempts": 2})
    state = FlowState(task_executions=[_exec(1, success=False)])
    ctx = GateContext(tasks=(_task(1, status="done"),))
    assert should_prompt("before_do_work", hf, state, ctx).should_prompt is False


def test_repeated_failure_multi_task_tie_break_longest_streak() -> None:
    hf = _conditional(repeated_task_failure={"enabled": True, "min_attempts": 2})
    state = FlowState(
        task_executions=[
            _exec(2, success=False),
            _exec(3, success=False),
            _exec(3, success=False),  # task 3 streak = 2 (worst)
        ]
    )
    ctx = GateContext(tasks=(_task(2), _task(3)))
    decision = should_prompt("before_do_work", hf, state, ctx)
    assert decision.should_prompt is True
    assert decision.trigger_reason is not None
    assert decision.trigger_reason.detail == RepeatedTaskFailureDetail(
        task_id=3, attempts=2
    )


def test_repeated_failure_tie_break_lowest_task_id_on_equal_streak() -> None:
    hf = _conditional(repeated_task_failure={"enabled": True, "min_attempts": 2})
    state = FlowState(
        task_executions=[_exec(5, success=False), _exec(4, success=False)]
    )
    ctx = GateContext(tasks=(_task(5), _task(4)))
    decision = should_prompt("before_do_work", hf, state, ctx)
    assert decision.trigger_reason is not None
    assert decision.trigger_reason.detail == RepeatedTaskFailureDetail(
        task_id=4, attempts=1
    )


# --- approaching_max_revisions -----------------------------------------------


def test_approaching_max_revisions_fires_within_window() -> None:
    hf = _conditional(approaching_max_revisions={"enabled": True, "within": 1})
    state = FlowState(revisions=1, max_revisions=2)  # 1 >= 2 - 1
    decision = should_prompt("after_review", hf, state)
    assert decision.should_prompt is True
    assert decision.trigger_reason is not None
    assert decision.trigger_reason.detail == ApproachingMaxRevisionsDetail(
        revisions=1, max_revisions=2
    )


def test_approaching_max_revisions_silent_outside_window() -> None:
    hf = _conditional(approaching_max_revisions={"enabled": True, "within": 1})
    state = FlowState(revisions=0, max_revisions=3)  # 0 >= 3 - 1 is False
    assert should_prompt("after_review", hf, state).should_prompt is False


def test_approaching_max_revisions_disabled_never_fires() -> None:
    hf = _conditional(approaching_max_revisions={"enabled": False, "within": 1})
    state = FlowState(revisions=1, max_revisions=2)  # would fire if enabled
    assert should_prompt("after_review", hf, state).should_prompt is False


def test_approaching_max_revisions_fires_at_ceiling() -> None:
    hf = _conditional(approaching_max_revisions={"enabled": True, "within": 1})
    state = FlowState(revisions=2, max_revisions=2)  # 2 >= 2 - 1
    assert should_prompt("after_review", hf, state).should_prompt is True


def test_trigger_only_fires_at_its_mapped_gate() -> None:
    # repeated_task_failure is mapped to before_do_work, not after_review.
    hf = _conditional(repeated_task_failure={"enabled": True, "min_attempts": 1})
    state = FlowState(task_executions=[_exec(1, success=False)])
    ctx = GateContext(tasks=(_task(1),))
    assert should_prompt("after_review", hf, state, ctx).should_prompt is False


# --- HumanFeedbackEntry.trigger_reason persistence ---------------------------


def test_human_feedback_entry_defaults_trigger_reason_to_none() -> None:
    entry = HumanFeedbackEntry.model_validate(_entry())
    assert entry.trigger_reason is None


def test_human_feedback_entry_loads_legacy_dict_without_trigger_reason() -> None:
    # A snapshot written before conditional HITL lacks the key entirely; it must
    # still validate (resume/backward-compat), not raise on the missing field.
    legacy = _entry()
    assert "trigger_reason" not in legacy
    entry = HumanFeedbackEntry.model_validate(legacy)
    assert entry.trigger_reason is None


def test_human_feedback_entry_round_trips_trigger_reason() -> None:
    reason = TriggerReason(
        kind="repeated_task_failure",
        detail=RepeatedTaskFailureDetail(task_id=3, attempts=2),
    )
    entry = HumanFeedbackEntry.model_validate(_entry(trigger_reason=reason))
    restored = HumanFeedbackEntry.model_validate(entry.model_dump())
    assert restored.trigger_reason == reason
    assert isinstance(restored.trigger_reason, TriggerReason)
    assert isinstance(restored.trigger_reason.detail, RepeatedTaskFailureDetail)


def test_human_feedback_entry_round_trips_approaching_max_revisions_detail() -> None:
    # Round-trip the *other* union member to prove smart-union disambiguation.
    reason = TriggerReason(
        kind="approaching_max_revisions",
        detail=ApproachingMaxRevisionsDetail(revisions=1, max_revisions=2),
    )
    entry = HumanFeedbackEntry.model_validate(_entry(trigger_reason=reason))
    restored = HumanFeedbackEntry.model_validate(entry.model_dump())
    assert restored.trigger_reason == reason
    assert isinstance(restored.trigger_reason, TriggerReason)
    assert isinstance(restored.trigger_reason.detail, ApproachingMaxRevisionsDetail)
