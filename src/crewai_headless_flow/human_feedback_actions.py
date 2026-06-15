"""Shared stage- and gate-scoped HITL action definitions and parsing helpers."""

from __future__ import annotations

from typing import Literal, cast

StageName = Literal["plan", "do_work", "review", "finalize"]
HumanFeedbackGate = Literal[
    "before_plan",
    "before_do_work",
    "before_review",
    "after_review",
    "before_finalize",
]

FLOW_STAGES: tuple[StageName, ...] = ("plan", "do_work", "review", "finalize")
MUTATING_STAGES: frozenset[StageName] = frozenset({"do_work", "finalize"})
DEFAULT_STAGE_GATES: dict[StageName, HumanFeedbackGate] = {
    "plan": "before_plan",
    "do_work": "before_do_work",
    "review": "before_review",
    "finalize": "before_finalize",
}
HUMAN_FEEDBACK_GATE_STAGES: dict[HumanFeedbackGate, StageName] = {
    "before_plan": "plan",
    "before_do_work": "do_work",
    "before_review": "review",
    "after_review": "review",
    "before_finalize": "finalize",
}
HUMAN_FEEDBACK_GATE_DEFAULTS: dict[HumanFeedbackGate, bool] = {
    "before_plan": False,
    "before_do_work": True,
    "before_review": False,
    "after_review": False,
    "before_finalize": True,
}
HUMAN_FEEDBACK_GATE_LABELS: dict[HumanFeedbackGate, str] = {
    "before_plan": "Before plan",
    "before_do_work": "Before do_work",
    "before_review": "Before review",
    "after_review": "After review",
    "before_finalize": "Before finalize",
}


HUMAN_FEEDBACK_STAGE_ACTIONS: dict[StageName, dict[str, tuple[str, ...]]] = {
    "do_work": {
        "skip-to-review": ("review", "skip-review", "skip-to-review"),
        "replan": ("replan", "plan", "replan-do-work"),
        "target-tasks": ("target", "target-tasks", "select-tasks"),
    },
    "review": {
        "force-revise": ("revise", "force-revise"),
        "replan": ("replan", "plan", "force-replan"),
        "force-pass": ("pass", "force-pass"),
        "rerun-review": ("rerun", "rerun-review", "retry-review"),
        "target-tasks": ("target", "target-tasks", "select-tasks"),
    },
    "finalize": {
        "skip-finalize": ("skip", "complete", "skip-finalize"),
        "force-revise": ("revise", "force-revise"),
        "replan": ("replan", "plan", "reopen-work"),
        "rerun-review": ("rerun", "rerun-review", "retry-review"),
        "target-tasks": ("target", "target-tasks", "select-tasks"),
    },
}

HUMAN_FEEDBACK_ACTION_GATES: dict[HumanFeedbackGate, StageName] = {
    "before_do_work": "do_work",
    "before_review": "review",
    "after_review": "review",
    "before_finalize": "finalize",
}

HUMAN_FEEDBACK_GATE_ACTIONS: dict[HumanFeedbackGate, tuple[str, ...]] = {
    "before_do_work": ("skip-to-review", "replan", "target-tasks"),
    "before_review": ("force-revise", "replan", "force-pass", "target-tasks"),
    "after_review": (
        "force-revise",
        "replan",
        "force-pass",
        "rerun-review",
        "target-tasks",
    ),
    "before_finalize": (
        "skip-finalize",
        "force-revise",
        "replan",
        "rerun-review",
        "target-tasks",
    ),
}

AFTER_REVIEW_GATE: HumanFeedbackGate = "after_review"


def supported_flow_stages() -> tuple[StageName, ...]:
    return FLOW_STAGES


def supported_human_feedback_stages() -> tuple[str, ...]:
    return tuple(HUMAN_FEEDBACK_STAGE_ACTIONS.keys())


def supported_human_feedback_gates() -> tuple[HumanFeedbackGate, ...]:
    return tuple(HUMAN_FEEDBACK_GATE_STAGES.keys())


def default_human_feedback_gate(stage: StageName) -> HumanFeedbackGate:
    return DEFAULT_STAGE_GATES[stage]


def default_human_feedback_gate_for_stage(stage: str) -> HumanFeedbackGate | None:
    if stage in DEFAULT_STAGE_GATES:
        return DEFAULT_STAGE_GATES[cast(StageName, stage)]
    return None


def human_feedback_gate_stage(gate: HumanFeedbackGate) -> StageName:
    return HUMAN_FEEDBACK_GATE_STAGES[gate]


def human_feedback_gate_position(
    gate: str,
) -> Literal["before", "after"] | None:
    if gate.startswith("before_"):
        return "before"
    if gate.startswith("after_"):
        return "after"
    return None


def human_feedback_gate_default_enabled(gate: HumanFeedbackGate) -> bool:
    return HUMAN_FEEDBACK_GATE_DEFAULTS[gate]


def human_feedback_gate_label(gate: HumanFeedbackGate) -> str:
    return HUMAN_FEEDBACK_GATE_LABELS[gate]


def stage_mutates_files(stage: StageName) -> bool:
    return stage in MUTATING_STAGES


def is_default_human_feedback_gate(stage: str, gate: str | None) -> bool:
    default_gate = default_human_feedback_gate_for_stage(stage)
    return gate is None or (default_gate is not None and gate == default_gate)


def human_feedback_target_label(
    stage: str,
    gate: str | None,
    *,
    include_default_gate: bool = False,
) -> str:
    resolved_gate = gate or default_human_feedback_gate_for_stage(stage)
    if resolved_gate is None:
        return stage
    if include_default_gate or not is_default_human_feedback_gate(stage, resolved_gate):
        return f"{stage}@{resolved_gate}"
    return stage


def is_after_review_gate(stage: str, gate: str | None) -> bool:
    return (
        stage == human_feedback_gate_stage(AFTER_REVIEW_GATE)
        and gate == AFTER_REVIEW_GATE
    )


def supported_human_feedback_action_targets() -> tuple[str, ...]:
    return tuple(HUMAN_FEEDBACK_STAGE_ACTIONS.keys()) + tuple(
        HUMAN_FEEDBACK_ACTION_GATES.keys()
    )


def resolve_human_feedback_action_stage(target: str) -> str | None:
    if target in HUMAN_FEEDBACK_STAGE_ACTIONS:
        return target
    if target in HUMAN_FEEDBACK_ACTION_GATES:
        return HUMAN_FEEDBACK_ACTION_GATES[target]
    return None


def supported_human_feedback_actions(target: str) -> tuple[str, ...]:
    if target in HUMAN_FEEDBACK_GATE_ACTIONS:
        return HUMAN_FEEDBACK_GATE_ACTIONS[target]
    if target in HUMAN_FEEDBACK_STAGE_ACTIONS:
        return tuple(HUMAN_FEEDBACK_STAGE_ACTIONS[target].keys())
    return ()


def human_feedback_action_prompt_token(stage: str, action: str) -> str:
    aliases: tuple[str, ...] = ()
    if stage in HUMAN_FEEDBACK_STAGE_ACTIONS:
        aliases = HUMAN_FEEDBACK_STAGE_ACTIONS[stage].get(action, ())
    alias = aliases[0] if aliases else action
    return f"{action}={alias}"


def parse_human_feedback_stage_action(
    stage: str,
    answer: str,
    enabled_actions: list[str],
) -> str | None:
    normalized = answer.strip().lower()
    stage_actions = (
        HUMAN_FEEDBACK_STAGE_ACTIONS[stage]
        if stage in HUMAN_FEEDBACK_STAGE_ACTIONS
        else {}
    )
    for action in enabled_actions:
        aliases = stage_actions.get(action, ())
        if normalized in aliases:
            return action
    return None
