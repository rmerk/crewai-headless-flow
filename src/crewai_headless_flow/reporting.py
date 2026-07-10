"""Deterministic execution/debug reporting from Flow state."""

from __future__ import annotations

import json

from .config import classify_stage_extra
from .human_feedback_actions import human_feedback_target_label
from .state import (
    FlowHistoryEntry,
    FlowState,
    HumanFeedbackEntry,
    StageRuntimeSnapshot,
    TaskExecutionEntry,
)
from .verification import VerificationReport


def render_execution_report(state: FlowState, history_limit: int = 10) -> str:
    lines = ["# Flow Execution Report", ""]
    lines.append(f"- Flow status: {state.status}")
    checkpoint = state.aborted_checkpoint
    if checkpoint is not None:
        lines.append(
            "- Aborted checkpoint: "
            + human_feedback_target_label(
                checkpoint.stage,
                checkpoint.gate,
                include_default_gate=True,
            )
        )
        if checkpoint.message:
            lines.append("- Aborted checkpoint message:")
            lines.extend(f"  {line}" for line in checkpoint.message.splitlines())
        if checkpoint.before_review_instructions:
            lines.append("- Saved before_review instructions:")
            lines.extend(
                f"  {line}"
                for line in checkpoint.before_review_instructions.splitlines()
            )
        if checkpoint.stage_input:
            lines.append(
                "- Saved resume input: "
                f"{len(checkpoint.stage_input)} chars across "
                f"{_text_line_count(checkpoint.stage_input)} line(s)"
            )
            lines.append("- Resume input preview:")
            lines.extend(
                f"  {line}" for line in _preview_text_lines(checkpoint.stage_input)
            )
        if (
            state.latest_work_summary
            and state.latest_work_summary != checkpoint.stage_input
        ):
            lines.append(
                "- Saved latest review input: "
                f"{len(state.latest_work_summary)} chars across "
                f"{_text_line_count(state.latest_work_summary)} line(s)"
            )
            lines.append("- Latest review input preview:")
            lines.extend(
                f"  {line}" for line in _preview_text_lines(state.latest_work_summary)
            )
    lines.append(f"- Review status: {state.review_status}")
    lines.append(f"- Revisions used: {state.revisions}/{state.max_revisions}")
    if state.pending_revision_replan:
        lines.append("- Pending revision replan: yes")
        if state.pending_revision_replan_reason:
            lines.append(f"  Reason: {state.pending_revision_replan_reason}")
    lines.append(f"- Changed files tracked: {len(state.changed_files)}")
    lines.append("")

    lines.append("## Runtime Configuration")
    runtime = _normalized_runtime(state)
    if not runtime:
        lines.append("- No runtime configuration recorded")
    else:
        for stage in runtime:
            model = stage.model or "(default)"
            lines.append(
                f"- {stage.stage}: skill={stage.skill} | worker={stage.worker} | "
                f"model={model} | timeout={stage.timeout}s | "
                f"mutates={'yes' if stage.can_mutate else 'no'}"
            )
            runtime_knobs = dict(stage.runtime_knobs)
            enforced_declarations = dict(stage.enforced_declarations)
            notes = list(stage.notes)
            if stage.extra and not runtime_knobs and not enforced_declarations:
                runtime_knobs, enforced_declarations, inferred_notes = (
                    classify_stage_extra(stage.stage, dict(stage.extra))
                )
                if not notes:
                    notes = inferred_notes
            if runtime_knobs:
                lines.append(
                    f"  Runtime knobs: {json.dumps(runtime_knobs, sort_keys=True)}"
                )
            if enforced_declarations:
                lines.append(
                    "  Enforced declarations: "
                    f"{json.dumps(enforced_declarations, sort_keys=True)}"
                )
            if notes:
                lines.append(f"  Notes: {', '.join(notes)}")
            if stage.extra:
                lines.append(f"  Raw extra: {json.dumps(stage.extra, sort_keys=True)}")
    if state.resolved_human_feedback:
        lines.append(
            f"- human_feedback: {json.dumps(state.resolved_human_feedback, sort_keys=True)}"
        )

    lines.append("")
    lines.append("## Tasks")
    if not state.tasks:
        lines.append("- No structured tasks")
    else:
        for task in state.tasks:
            title = task.title or task.description
            lines.append(f"- Task {task.id} [{task.status}]: {title}")
            if task.files:
                lines.append(f"  Files: {', '.join(task.files)}")
            if task.review_notes:
                lines.append(f"  Review notes: {'; '.join(task.review_notes)}")
            if task.last_error:
                lines.append(f"  Last error: {task.last_error}")

    lines.append("")
    lines.append("## Human Feedback")
    feedback_log = _normalized_human_feedback(state)
    if not feedback_log:
        lines.append("- None")
    else:
        for feedback_entry in feedback_log:
            decision = feedback_entry.action or (
                "approve" if feedback_entry.approved else "abort"
            )
            lines.append(
                "- r"
                f"{feedback_entry.revision} "
                f"{human_feedback_target_label(feedback_entry.stage, feedback_entry.gate)} "
                f"{decision} "
                f"worker={feedback_entry.worker} response={feedback_entry.response}"
            )
            if feedback_entry.task_ids:
                lines.append(
                    "  Target tasks: "
                    + ", ".join(str(task_id) for task_id in feedback_entry.task_ids)
                )
            if feedback_entry.instructions:
                lines.append(f"  Instructions: {feedback_entry.instructions}")

    lines.append("")
    lines.append("## Review Issues")
    if state.issues:
        lines.extend(f"- {issue}" for issue in state.issues)
    else:
        lines.append("- None")

    lines.append("")
    lines.append("## Verification")
    verification_runs = _normalized_verification_runs(state)
    if not verification_runs:
        lines.append("- None")
    else:
        for run in verification_runs[-history_limit:]:
            lines.append(
                f"- r{run.revision} mode={run.mode} "
                f"passed={'yes' if run.passed else 'no'} "
                f"commands={len(run.results)}: {run.message}"
            )
            for result in run.results:
                if result.exit_code == 0:
                    continue
                lines.append(f"  exit={result.exit_code} `{result.command}`")
                if result.output_tail:
                    lines.extend(
                        f"    {tail_line}"
                        for tail_line in _preview_text_lines(result.output_tail)
                    )

    lines.append("")
    lines.append("## Review Targets")
    if state.review_task_hints:
        for hint in state.review_task_hints:
            task_ids = list(getattr(hint, "task_ids", []))
            files = list(getattr(hint, "files", []))
            summary = str(getattr(hint, "summary", ""))
            target_bits = []
            if task_ids:
                target_bits.append(
                    f"tasks={','.join(str(task_id) for task_id in task_ids)}"
                )
            if files:
                target_bits.append(f"files={','.join(files)}")
            target_text = " | ".join(target_bits) if target_bits else "unmapped"
            lines.append(f"- {target_text}: {summary}")
    else:
        lines.append("- None")

    lines.append("")
    lines.append("## Task Executions")
    executions = _normalized_task_executions(state)
    if not executions:
        lines.append("- None")
    else:
        for execution in executions[-history_limit:]:
            model = execution.model or "(default)"
            batch_text = (
                f" batch={execution.parallel_batch_id}"
                if execution.parallel_batch_id
                else ""
            )
            isolated_text = "yes" if execution.isolated_workspace else "no"
            lines.append(
                f"- task={execution.task_id} attempt={execution.attempt} "
                f"r{execution.revision} path={execution.orchestration} "
                f"success={'yes' if execution.success else 'no'} "
                f"worker={execution.worker} model={model}"
                f"{batch_text} isolated={isolated_text}"
            )
            if execution.changed_files:
                lines.append(f"  Changed files: {', '.join(execution.changed_files)}")
            if execution.workspace:
                lines.append(f"  Workspace: {execution.workspace}")
            if execution.error:
                lines.append(f"  Error: {execution.error}")
            if execution.summary:
                lines.append(f"  Summary: {execution.summary}")
            for round_entry in execution.crew_rounds:
                subtask_text = ""
                if round_entry.subtask_id is not None:
                    subtask_title = (
                        f": {round_entry.subtask_title}"
                        if round_entry.subtask_title
                        else ""
                    )
                    subtask_text = f" [subtask {round_entry.subtask_id}{subtask_title}]"
                issues = (
                    f" | issues={'; '.join(round_entry.decision_issues)}"
                    if round_entry.decision_issues
                    else ""
                )
                lines.append(
                    f"  Round {round_entry.round}{subtask_text}: {round_entry.decision_status}"
                    f" | {round_entry.decision_summary}{issues}"
                )

    lines.append("")
    lines.append("## Recent History")
    history = _normalized_history(state)
    if not history:
        lines.append("- None")
    else:
        for history_entry in history[-history_limit:]:
            task_text = (
                f" tasks={','.join(str(task_id) for task_id in history_entry.task_ids)}"
                if history_entry.task_ids
                else ""
            )
            file_text = (
                f" files={','.join(history_entry.files)}" if history_entry.files else ""
            )
            lines.append(
                f"- r{history_entry.revision} {history_entry.kind}{task_text}{file_text}: "
                f"{history_entry.summary}"
            )
            lines.extend(f"  - {detail}" for detail in history_entry.details[:3])

    return "\n".join(lines).strip()


def _text_line_count(value: str) -> int:
    return max(1, len(value.splitlines()))


def _preview_text_lines(
    value: str, *, max_lines: int = 6, max_chars_per_line: int = 120
) -> list[str]:
    lines = value.splitlines() or [value]
    preview = [
        line
        if len(line) <= max_chars_per_line
        else f"{line[: max_chars_per_line - 3]}..."
        for line in lines[:max_lines]
    ]
    if len(lines) > max_lines:
        preview.append(f"... ({len(lines) - max_lines} more lines)")
    return preview


def _normalized_history(state: FlowState) -> list[FlowHistoryEntry]:
    history: list[FlowHistoryEntry] = []
    for entry in state.history:
        if isinstance(entry, FlowHistoryEntry):
            history.append(entry)
        elif isinstance(entry, dict):
            history.append(FlowHistoryEntry.model_validate(entry))
    return history


def _normalized_runtime(state: FlowState) -> list[StageRuntimeSnapshot]:
    runtime: list[StageRuntimeSnapshot] = []
    for entry in state.resolved_stages:
        if isinstance(entry, StageRuntimeSnapshot):
            runtime.append(entry)
        elif isinstance(entry, dict):
            runtime.append(StageRuntimeSnapshot.model_validate(entry))
    return runtime


def _normalized_human_feedback(state: FlowState) -> list[HumanFeedbackEntry]:
    feedback: list[HumanFeedbackEntry] = []
    for entry in state.human_feedback_log:
        if isinstance(entry, HumanFeedbackEntry):
            feedback.append(entry)
        elif isinstance(entry, dict):
            feedback.append(HumanFeedbackEntry.model_validate(entry))
    return feedback


def _normalized_verification_runs(state: FlowState) -> list[VerificationReport]:
    runs: list[VerificationReport] = []
    for entry in state.verification_runs:
        if isinstance(entry, VerificationReport):
            runs.append(entry)
        elif isinstance(entry, dict):
            runs.append(VerificationReport.model_validate(entry))
    return runs


def _normalized_task_executions(state: FlowState) -> list[TaskExecutionEntry]:
    executions: list[TaskExecutionEntry] = []
    for entry in state.task_executions:
        if isinstance(entry, TaskExecutionEntry):
            executions.append(entry)
        elif isinstance(entry, dict):
            executions.append(TaskExecutionEntry.model_validate(entry))
    return executions
