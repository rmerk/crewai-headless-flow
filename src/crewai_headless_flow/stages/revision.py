"""Revision stage body and helpers (Phase 1 extraction)."""

from __future__ import annotations

import logging
import re

from ..review_contract import ReviewTaskHint
from ..review_crew import ReviewCrewDecision
from ..state import TaskItem

logger = logging.getLogger(__name__)


def execute_process_revision(flow, decision: str) -> str:
    if flow._is_terminal_status():
        logger.info(
            f"[Flow] Skipping revise because flow is terminal: {flow.state.status}"
        )
        return flow._terminal_result()
    flow._mark_running()
    flow._log_event("stage_start", stage="revise")
    flow.state.increment_revision()
    logger.info(
        f"\n[Flow] Revising (revision {flow.state.revisions}/{flow.state.max_revisions})"
    )

    if flow.state.revisions >= flow.state.max_revisions:
        message = "Max revisions reached before review could pass."
        logger.warning("[Flow] Max revisions reached. Marking flow failed.")
        flow.state.last_stage = "revise"
        flow.state.status = "failed"
        flow.state.review_status = "revise"
        flow.state.issues = [*flow.state.issues, message]
        flow.state.errors.append(message)
        flow._log_event("run_failed", reason=message)
        flow._refresh_debug_report()
        flow._record_history(
            kind="review_decision",
            summary="Flow failed after max revisions.",
            task_ids=sorted(
                task.id for task in flow.state.tasks if task.status != "done"
            ),
            details=flow.state.issues[:3],
        )
        return "failed"

    if flow.state.tasks:
        do_work_worker = flow._get_worker("do_work")
        if flow._attempt_structured_revision_replan(do_work_worker):
            active_ids = [task.id for task in flow.state.tasks if task.status != "done"]
            return flow._build_structured_revision_prompt(active_ids)
        return flow._prepare_structured_revision_prompt()

    # Loop back to do_work with the issues as additional context
    issues_text = "\n".join(f"- {i}" for i in flow.state.issues)
    return (
        f"Previous review found the following issues that must be fixed:\n{issues_text}"
    )


def planned_task_review_context(flow) -> str:
    if not flow.state.tasks:
        return "- No structured tasks available"

    lines = []
    for task in flow.state.tasks:
        files = ", ".join(task.files) if task.files else "n/a"
        lines.append(
            f"- Task {task.id}: {task.title or task.description} | status={task.status} | files={files}"
        )
    return "\n".join(lines)


def review_target_summary(flow) -> str:
    hints = flow._normalized_review_task_hints()
    if not hints:
        return "- None"

    lines: list[str] = []
    for hint in hints:
        target_bits = []
        if hint.task_ids:
            target_bits.append(
                f"tasks={','.join(str(task_id) for task_id in hint.task_ids)}"
            )
        if hint.files:
            target_bits.append(f"files={','.join(hint.files)}")
        target_text = " | ".join(target_bits) if target_bits else "unmapped"
        lines.append(f"- {target_text}: {hint.summary}")
    return "\n".join(lines)


def task_files_for_ids(flow, task_ids: list[int]) -> list[str]:
    selected = []
    wanted = set(task_ids)
    for task in flow.state.tasks:
        if task.id not in wanted:
            continue
        for path in task.files:
            if path not in selected:
                selected.append(path)
    return selected


def task_ids_for_file_selector(flow, raw_selector: str) -> list[int]:
    wanted = raw_selector.strip()
    if not wanted:
        return []

    selected: list[int] = []
    for task in flow.state.tasks:
        task_files = [path.strip() for path in task.files if path.strip()]
        if wanted in task_files:
            selected.append(task.id)
    return selected


def hinted_task_ids(flow) -> list[int]:
    task_map = {task.id: task for task in flow.state.tasks}
    target_ids: set[int] = set()

    for hint in flow._normalized_review_task_hints():
        for task_id in hint.task_ids:
            if task_id in task_map:
                target_ids.add(task_id)
        if hint.files:
            hint_files = {path.strip() for path in hint.files if path.strip()}
            for task in flow.state.tasks:
                task_files = {path.strip() for path in task.files if path.strip()}
                if hint_files & task_files:
                    target_ids.add(task.id)

    return sorted(target_ids)


def prepare_structured_revision_prompt(flow) -> str:
    targeted_ids = flow._mark_tasks_for_revision()
    return flow._build_structured_revision_prompt(targeted_ids)


def build_structured_revision_prompt(flow, targeted_ids: list[int]) -> str:
    issues_text = "\n".join(f"- {issue}" for issue in flow.state.issues)
    target_lines = "\n".join(
        flow._revision_target_line(task)
        for task in flow.state.tasks
        if task.id in set(targeted_ids)
    )

    return (
        "Previous review found issues that must be fixed.\n"
        f"Target tasks for this revision:\n{target_lines or '- All completed tasks'}\n\n"
        f"Review issues:\n{issues_text}\n\n"
        f"Recent history:\n{flow._history_summary(limit=6)}"
    )


def revision_target_line(flow, task: TaskItem) -> str:
    notes = "; ".join(task.review_notes) if task.review_notes else "No specific note."
    return f"- Task {task.id} ({task.title or task.description}): {notes}"


def mark_tasks_for_revision(flow) -> list[int]:
    targeted_ids = flow._collect_target_task_ids()
    notes_by_task = flow._build_revision_notes_by_task(targeted_ids)
    downstream_ids = flow._expand_dependent_task_ids(targeted_ids)

    for task in flow.state.tasks:
        if task.id in targeted_ids:
            task.status = "needs_revision"
            task.review_notes = notes_by_task.get(task.id, flow.state.issues.copy())
        elif task.id in downstream_ids and task.status == "done":
            task.status = "needs_revision"
            task.review_notes = ["Upstream dependency requires revision."]

    tracked_ids = sorted(targeted_ids | downstream_ids)
    flow._record_history(
        kind="revision_targeting",
        summary="Prepared targeted revision batch.",
        task_ids=tracked_ids,
        details=[
            flow._revision_target_line(task)
            for task in flow.state.tasks
            if task.id in tracked_ids
        ],
    )
    return tracked_ids


def collect_target_task_ids(flow) -> set[int]:
    task_map = {task.id: task for task in flow.state.tasks}
    target_ids: set[int] = set()

    for hint in flow._normalized_review_task_hints():
        for task_id in hint.task_ids:
            if task_id in task_map:
                target_ids.add(task_id)
        if hint.files:
            hint_files = {path.strip() for path in hint.files if path.strip()}
            for task in flow.state.tasks:
                task_files = {path.strip() for path in task.files if path.strip()}
                if hint_files & task_files:
                    target_ids.add(task.id)

    if target_ids:
        return target_ids

    done_or_failed = {
        task.id for task in flow.state.tasks if task.status in {"done", "failed"}
    }
    return done_or_failed or {task.id for task in flow.state.tasks}


def build_revision_notes_by_task(flow, target_ids: set[int]) -> dict[int, list[str]]:
    notes_by_task: dict[int, list[str]] = {task_id: [] for task_id in target_ids}

    for hint in flow._normalized_review_task_hints():
        matched_ids = {task_id for task_id in hint.task_ids if task_id in target_ids}
        if hint.files:
            hint_files = {path.strip() for path in hint.files if path.strip()}
            for task in flow.state.tasks:
                task_files = {path.strip() for path in task.files if path.strip()}
                if task.id in target_ids and hint_files & task_files:
                    matched_ids.add(task.id)
        for task_id in matched_ids:
            notes_by_task.setdefault(task_id, []).append(hint.summary)

    return notes_by_task


def normalized_review_task_hints(flow) -> list[ReviewTaskHint]:
    hints: list[ReviewTaskHint] = []
    for hint in flow.state.review_task_hints:
        if isinstance(hint, ReviewTaskHint):
            hints.append(hint)
        elif isinstance(hint, dict):
            hints.append(ReviewTaskHint.model_validate(hint))
    return hints


def infer_review_task_hints(flow, decision: ReviewCrewDecision) -> list[ReviewTaskHint]:
    hints: list[ReviewTaskHint] = []
    combined_text = [decision.summary, *decision.issues]

    for text in combined_text:
        normalized_text = text.lower()
        matched_ids: set[int] = set()
        matched_files: set[str] = set()

        for task in flow.state.tasks:
            task_patterns = (
                rf"\btask\s+#?{task.id}\b",
                rf"\btask[- ]{task.id}\b",
                rf"\b#{task.id}\b",
            )
            if any(re.search(pattern, normalized_text) for pattern in task_patterns):
                matched_ids.add(task.id)

            task_title = (task.title or task.description).strip().lower()
            if task_title and len(task_title) > 8 and task_title in normalized_text:
                matched_ids.add(task.id)

            for path in task.files:
                normalized_path = path.strip().lower()
                if normalized_path and normalized_path in normalized_text:
                    matched_ids.add(task.id)
                    matched_files.add(path)

        if matched_ids or matched_files:
            hints.append(
                ReviewTaskHint(
                    task_ids=sorted(matched_ids),
                    files=sorted(matched_files),
                    summary=text,
                )
            )

    deduped: list[ReviewTaskHint] = []
    seen: set[tuple[tuple[int, ...], tuple[str, ...], str]] = set()
    for hint in hints:
        key = (tuple(hint.task_ids), tuple(hint.files), hint.summary)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(hint)

    return deduped


def expand_dependent_task_ids(flow, seed_ids: set[int]) -> set[int]:
    expanded = set(seed_ids)
    changed = True

    while changed:
        changed = False
        for task in flow.state.tasks:
            if task.id in expanded or task.status != "done":
                continue
            if any(dependency in expanded for dependency in task.dependencies):
                expanded.add(task.id)
                changed = True

    return expanded - seed_ids
