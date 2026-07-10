"""
Pydantic state for the main CrewAI Headless Flow.

Persistence: when a run directory is configured (``--runs-dir``), the Flow
checkpoints this state as JSON into ``runs/<run_id>/state.json`` at every
state mutation via ``run_store.RunStore`` — CrewAI's ``@persist`` decorator is
deliberately NOT used, because the bespoke resume path replays stage methods
against a rehydrated state and a second persistence source of truth would
compete with it. State also round-trips through the CLI's optional
``--state-file`` / ``--resume-state-file`` flags.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, model_serializer, model_validator

from .human_feedback_actions import StageName
from .review_contract import ReviewTaskHint


# --- Conditional-HITL trigger reasons (persisted on HumanFeedbackEntry) ------
# These describe *why* a conditional gate fired. They live in the state layer
# (not in ``hitl_policy``) because they are persisted audit data carried on
# ``HumanFeedbackEntry``; ``hitl_policy`` imports them. Keeping them here also
# avoids a circular import, since ``hitl_policy`` already imports ``FlowState``/
# ``TaskItem``/``TaskExecutionEntry`` from this module.


@dataclass(frozen=True)
class RepeatedTaskFailureDetail:
    """Why ``repeated_task_failure`` fired: which task, and its failure streak."""

    task_id: int
    #: consecutive failures since the task's last success (the streak that
    #: tripped the threshold), i.e. the count compared against ``min_attempts``.
    attempts: int


@dataclass(frozen=True)
class ApproachingMaxRevisionsDetail:
    """Why ``approaching_max_revisions`` fired: the revise-loop position."""

    revisions: int
    max_revisions: int


#: Discriminated union of per-trigger detail payloads; ``TriggerReason.kind``
#: names which member ``detail`` holds.
TriggerDetail = RepeatedTaskFailureDetail | ApproachingMaxRevisionsDetail


@dataclass(frozen=True)
class TriggerReason:
    """Structured reason a conditional gate fired.

    ``kind`` mirrors the trigger's config key 1:1; ``detail`` is the typed
    payload for that kind. Modeled on LaunchDarkly's ``reason.kind`` + detail
    evaluation-reason pattern so the audit trail stays machine-readable.
    """

    kind: str
    detail: TriggerDetail


class StageRuntimeSnapshot(BaseModel):
    """Resolved runtime configuration for a single flow stage."""

    stage: str
    skill: str
    worker: str
    model: str | None = None
    timeout: int = 300
    extra: dict[str, object] = Field(default_factory=dict)
    runtime_knobs: dict[str, object] = Field(default_factory=dict)
    enforced_declarations: dict[str, object] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    can_mutate: bool = False


class HumanFeedbackEntry(BaseModel):
    """Persisted audit entry for a prompted HITL decision."""

    stage: str
    gate: str | None = None
    approved: bool
    action: str | None = None
    response: str
    instructions: str | None = None
    task_ids: list[int] = Field(default_factory=list)
    revision: int = 0
    worker: str
    skill: str
    can_mutate: bool = False
    message: str
    #: Why a conditional gate fired (``None`` for static gates). Defaults to
    #: ``None`` so state snapshots persisted before conditional HITL existed —
    #: i.e. dicts lacking this key entirely — resume without modification.
    trigger_reason: TriggerReason | None = None


class TaskItem(BaseModel):
    """A single task from the breakdown."""

    id: int
    title: str | None = None
    description: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    dependencies: list[int] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    estimated_scope: str | None = None
    review_notes: list[str] = Field(default_factory=list)
    last_error: str | None = None
    status: Literal["pending", "in_progress", "needs_revision", "failed", "done"] = (
        "pending"
    )


class FlowHistoryEntry(BaseModel):
    """Compact persisted history for execution/review/revision debugging."""

    kind: Literal[
        "batch_planning",
        "execution_targeting",
        "execution_replanning",
        "human_replanning",
        "revision_replanning",
        "task_complete",
        "task_failed",
        "review_decision",
        "revision_targeting",
    ]
    revision: int = 0
    summary: str
    task_ids: list[int] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list)


class CrewRoundEntry(BaseModel):
    """One bounded Implementation Crew round for a task attempt."""

    round: int
    subtask_id: int | None = None
    subtask_title: str | None = None
    decision_status: Literal["pass", "revise"]
    decision_summary: str
    decision_issues: list[str] = Field(default_factory=list)
    result_summary: str = ""
    result_error: str | None = None


class TaskExecutionEntry(BaseModel):
    """Persisted per-task execution telemetry for runtime debugging."""

    task_id: int
    attempt: int
    revision: int = 0
    worker: str
    model: str | None = None
    orchestration: Literal["direct", "crew"] = "direct"
    success: bool
    summary: str
    error: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    isolated_workspace: bool = False
    workspace: str | None = None
    parallel_batch_id: str | None = None
    crew_rounds: list[CrewRoundEntry] = Field(default_factory=list)


class AbortedCheckpoint(BaseModel):
    """Persisted human-aborted checkpoint snapshot used by resume paths."""

    stage: StageName
    gate: str | None = None
    message: str | None = None
    before_review_instructions: str | None = None
    stage_input: str | None = None


class FlowState(BaseModel):
    """
    Structured state for the entire reusable headless coding flow.
    """

    # Run identity (populated when a run directory is configured)
    run_id: str | None = None
    run_dir: str | None = None
    created_at: str | None = None

    # Inputs
    request: str = ""
    target_repo: str = ""
    config_dir: str | None = None
    resolved_stages: list[StageRuntimeSnapshot] = Field(default_factory=list)
    resolved_human_feedback: dict[str, object] = Field(default_factory=dict)

    # Plan stage output
    spec: str | None = None
    tasks: list[TaskItem] = Field(default_factory=list)

    # Work + review state
    changed_files: list[str] = Field(default_factory=list)
    latest_work_summary: str | None = None
    review_status: Literal["pending", "pass", "revise"] = "pending"
    issues: list[str] = Field(default_factory=list)
    review_task_hints: list[ReviewTaskHint] = Field(default_factory=list)
    human_feedback_log: list[HumanFeedbackEntry] = Field(default_factory=list)
    pending_revision_replan: bool = False
    pending_revision_replan_reason: str | None = None

    # Bounded revise loop control
    revisions: int = 0
    max_revisions: int = 2

    # Final output
    final_artifact: str | None = None
    debug_report: str | None = None

    # Internal / diagnostics
    status: Literal["pending", "running", "completed", "aborted_by_human", "failed"] = (
        "pending"
    )
    aborted_checkpoint: AbortedCheckpoint | None = None
    last_stage: str | None = None
    errors: list[str] = Field(default_factory=list)
    history: list[FlowHistoryEntry] = Field(default_factory=list)
    task_executions: list[TaskExecutionEntry] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _upgrade_legacy_aborted_checkpoint(cls, data: Any) -> Any:
        if not isinstance(data, dict) or data.get("aborted_checkpoint") is not None:
            return data

        stage = data.get("aborted_stage")
        gate = data.get("aborted_gate")
        message = data.get("aborted_gate_message")
        before_review_instructions = data.get("aborted_before_review_instructions")
        stage_input = data.get("aborted_stage_input")
        if not any(
            value is not None
            for value in (
                stage,
                gate,
                message,
                before_review_instructions,
                stage_input,
            )
        ):
            return data
        if stage is None:
            return data

        upgraded = dict(data)
        upgraded["aborted_checkpoint"] = {
            "stage": stage,
            "gate": gate,
            "message": message,
            "before_review_instructions": before_review_instructions,
            "stage_input": stage_input,
        }
        return upgraded

    def set_aborted_checkpoint(
        self,
        *,
        stage: StageName,
        gate: str | None = None,
        message: str | None = None,
        before_review_instructions: str | None = None,
        stage_input: str | None = None,
    ) -> None:
        self.aborted_checkpoint = AbortedCheckpoint(
            stage=stage,
            gate=gate,
            message=message,
            before_review_instructions=before_review_instructions,
            stage_input=stage_input,
        )

    def clear_aborted_checkpoint(self) -> None:
        self.aborted_checkpoint = None

    @property
    def aborted_stage(self) -> str | None:
        checkpoint = self.aborted_checkpoint
        return checkpoint.stage if checkpoint is not None else None

    @property
    def aborted_gate(self) -> str | None:
        checkpoint = self.aborted_checkpoint
        return checkpoint.gate if checkpoint is not None else None

    @property
    def aborted_gate_message(self) -> str | None:
        checkpoint = self.aborted_checkpoint
        return checkpoint.message if checkpoint is not None else None

    @property
    def aborted_before_review_instructions(self) -> str | None:
        checkpoint = self.aborted_checkpoint
        return checkpoint.before_review_instructions if checkpoint is not None else None

    @property
    def aborted_stage_input(self) -> str | None:
        checkpoint = self.aborted_checkpoint
        return checkpoint.stage_input if checkpoint is not None else None

    @model_serializer(mode="wrap")
    def _serialize_with_legacy_aborted_fields(self, handler):
        data = handler(self)
        data["aborted_stage"] = self.aborted_stage
        data["aborted_gate"] = self.aborted_gate
        data["aborted_gate_message"] = self.aborted_gate_message
        data["aborted_before_review_instructions"] = (
            self.aborted_before_review_instructions
        )
        data["aborted_stage_input"] = self.aborted_stage_input
        return data

    @property
    def should_revise(self) -> bool:
        return self.review_status == "revise" and self.revisions < self.max_revisions

    def increment_revision(self) -> None:
        self.revisions += 1
        self.review_status = "pending"  # reset for next round
