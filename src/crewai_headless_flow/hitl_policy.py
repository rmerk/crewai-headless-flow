"""Conditional Human-in-the-Loop policy: decide *whether* a gate should prompt.

This is the single seam that ``flow.py`` consults at each human-feedback
checkpoint. In ``mode: "static"`` it reproduces the legacy behavior (the gate's
boolean decides). In ``mode: "conditional"`` the legacy booleans are ignored and
gate-firing is driven entirely by deterministic, state-derived triggers.

The module is intentionally pure: ``should_prompt`` and every trigger evaluator
are functions over ``FlowState`` plus a small ``GateContext`` — no I/O, no
adapters, no LLM — so the whole policy is exhaustively offline-testable.

Trigger→gate mapping is **hardcoded** (see ``_TRIGGERS``), not configurable:
every Phase 0 trigger has exactly one sensible gate, so a configurable field
with one legal value would be speculative generality and a footgun. ``_TRIGGERS``
is the single source of truth pairing each trigger name with its gate and
evaluator; ``TriggerReason.kind`` is derived from the registry key, so a new
trigger is registered in exactly one place.

Phase 0 ships exactly two triggers:
  * ``repeated_task_failure``      → ``before_do_work``
  * ``approaching_max_revisions``  → ``after_review``
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .human_feedback_actions import HumanFeedbackGate
from .state import (
    ApproachingMaxRevisionsDetail,
    FlowState,
    RepeatedTaskFailureDetail,
    TaskExecutionEntry,
    TaskItem,
    TriggerDetail,
    TriggerReason,
)

# --- Gate call-site inputs and the policy's answer ---------------------------


@dataclass(frozen=True)
class GateContext:
    """Call-site state a gate needs beyond what already lives on ``FlowState``.

    Only ``before_do_work`` populates ``tasks`` in Phase 0 (its trigger scans
    the current task set before ``execution_target_task_ids`` is resolved); the
    other gates pass an empty context.
    """

    tasks: tuple[TaskItem, ...] = ()


@dataclass(frozen=True)
class GateDecision:
    """The policy's answer: prompt or not, and (if conditional) why."""

    should_prompt: bool
    #: ``None`` when a static gate — or no trigger — drove the decision.
    trigger_reason: TriggerReason | None = None


# --- Safe, ``Any``-free extraction from the untyped config mapping -----------


def _as_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _as_bool(value: object) -> bool:
    return value if isinstance(value, bool) else False


def _as_positive_int(value: object, default: int) -> int:
    # ``bool`` is an ``int`` subclass — exclude it explicitly. Non-positive or
    # non-int values fall back to ``default``; the config layer rejects them at
    # the boundary, so this is defense-in-depth, not a supported input path.
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if value >= 1 else default


# --- Trigger evaluators (return the typed detail that fired, or ``None``) -----


def _consecutive_failures(state: FlowState, task_id: int) -> int:
    """Consecutive failures since ``task_id``'s last success.

    Walks ``task_executions`` backward from the most recent entry, counting
    failures until the first success. This deliberately does *not* count total
    executions: a task that succeeded once and was reopened for revision rework
    must not look identical to one stuck genuinely failing. Streaks span
    revisions on purpose — repeated failure is a property of the task across the
    whole run, not of a single revision round.
    """

    count = 0
    for raw in reversed(state.task_executions):
        entry = (
            raw
            if isinstance(raw, TaskExecutionEntry)
            else TaskExecutionEntry.model_validate(raw)
        )
        if entry.task_id != task_id:
            continue
        if entry.success:
            break
        count += 1
    return count


def _eval_repeated_task_failure(
    trigger: Mapping[str, object], state: FlowState, context: GateContext
) -> TriggerDetail | None:
    """Fire if any not-yet-done task's failure streak meets the threshold.

    ``min_attempts`` names the *attempt number* that should warn, not a failure
    count: ``min_attempts: 2`` fires before a task's 2nd attempt — i.e. after
    exactly 1 prior failure (``streak >= min_attempts - 1``). When several tasks
    are simultaneously over threshold, the worst offender wins: longest streak,
    then lowest ``task_id`` as a deterministic secondary key.
    """

    min_attempts = _as_positive_int(trigger.get("min_attempts"), 2)
    threshold = min_attempts - 1

    candidates: list[tuple[int, int]] = []  # (streak, task_id)
    for task in context.tasks:
        if task.status == "done":
            continue
        streak = _consecutive_failures(state, task.id)
        if streak >= threshold:
            candidates.append((streak, task.id))

    if not candidates:
        return None

    streak, task_id = min(candidates, key=lambda sc: (-sc[0], sc[1]))
    return RepeatedTaskFailureDetail(task_id=task_id, attempts=streak)


def _eval_approaching_max_revisions(
    trigger: Mapping[str, object], state: FlowState, context: GateContext
) -> TriggerDetail | None:
    """Fire when the revise loop is within ``within`` of its ceiling."""

    within = _as_positive_int(trigger.get("within"), 1)
    if state.revisions >= state.max_revisions - within:
        return ApproachingMaxRevisionsDetail(
            revisions=state.revisions,
            max_revisions=state.max_revisions,
        )
    return None


# --- Trigger registry: single source of truth for name → (gate, evaluator) ---

_Evaluator = Callable[
    [Mapping[str, object], FlowState, GateContext], "TriggerDetail | None"
]

_TRIGGERS: dict[str, tuple[HumanFeedbackGate, _Evaluator]] = {
    "repeated_task_failure": ("before_do_work", _eval_repeated_task_failure),
    "approaching_max_revisions": ("after_review", _eval_approaching_max_revisions),
}


# --- Public entry point ------------------------------------------------------


def should_prompt(
    gate: HumanFeedbackGate,
    hf_config: Mapping[str, object],
    state: FlowState,
    context: GateContext = GateContext(),
) -> GateDecision:
    """Decide whether ``gate`` should prompt the operator.

    ``mode: "static"`` (default): the gate's legacy boolean decides, reproducing
    pre-conditional behavior exactly (missing gate defaults to ``True``, matching
    the historical ``hf.get(gate, True)``).

    ``mode: "conditional"``: legacy gate booleans are ignored entirely; the gate
    fires iff an enabled trigger mapped to it meets its threshold. ``kind`` on
    the resulting ``TriggerReason`` is the registry key that fired.
    """

    if hf_config.get("mode", "static") != "conditional":
        return GateDecision(should_prompt=_as_bool(hf_config.get(gate, True)))

    triggers = _as_mapping(_as_mapping(hf_config.get("conditional")).get("triggers"))
    for name, (trigger_gate, evaluate) in _TRIGGERS.items():
        if trigger_gate != gate:
            continue
        trigger_cfg = _as_mapping(triggers.get(name))
        if not _as_bool(trigger_cfg.get("enabled")):
            continue
        detail = evaluate(trigger_cfg, state, context)
        if detail is not None:
            return GateDecision(
                should_prompt=True,
                trigger_reason=TriggerReason(kind=name, detail=detail),
            )

    return GateDecision(should_prompt=False)


__all__ = [
    "ApproachingMaxRevisionsDetail",
    "GateContext",
    "GateDecision",
    "RepeatedTaskFailureDetail",
    "TriggerDetail",
    "TriggerReason",
    "should_prompt",
]
