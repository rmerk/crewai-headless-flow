"""Do-work stage body and helpers (Phase 1 extraction)."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..do_work_batch_contract import (
    DO_WORK_BATCH_PLAN_SCHEMA,
    normalize_do_work_batch_plan,
    require_concrete_do_work_batch_plan,
)
from ..do_work_crew import run_do_work_crew
from ..plan_contract import (
    plan_tasks_to_state_items,
    state_items_to_plan_output,
    render_plan_markdown,
    require_concrete_plan,
    normalize_plan_output,
    PlanOutput,
)
from ..paths_policy import match_denied, restore_denied_paths
from ..state import CrewRoundEntry, TaskItem, TaskExecutionEntry
from ..task_batches import (
    has_pending_tasks,
    ready_execution_tasks,
    select_execution_batch,
)
from ..tools.coder_tool import HeadlessCoderTool
from ..workers.base import CoderResult
from ..workspace_changes import (
    apply_changed_files,
    cleanup_workspace_copy,
    create_workspace_copy,
    diff_workspace_snapshots,
    snapshot_workspace,
)

logger = logging.getLogger(__name__)


@dataclass
class ParallelTaskOutcome:
    task: TaskItem
    summary: str
    success: bool
    changed_files: list[str]
    error: str | None = None
    workspace: str | None = None


@dataclass
class PlannedBatchSelection:
    task_ids: list[int]
    hinted_files: dict[int, list[str]]
    summary: str
    planned_files: list[str]


def parallel_do_work_enabled(flow, stage_cfg) -> bool:
    parallel_cfg = stage_cfg.extra.get("parallel", {}) or {}
    return bool(parallel_cfg.get("enabled", False))


def do_work_crew_enabled(flow, stage_cfg) -> bool:
    crew_cfg = stage_cfg.extra.get("crew", {}) or {}
    return bool(crew_cfg.get("enabled", False))


def build_task_execution_prompt(
    flow,
    task: TaskItem,
    plan_output: str,
    human_instructions: str | None = None,
) -> str:
    acceptance = "\n".join(f"- {criterion}" for criterion in task.acceptance_criteria)
    verification = "\n".join(f"- {check}" for check in task.verification)
    files = "\n".join(f"- {path}" for path in task.files)
    dependencies = ", ".join(str(dep) for dep in task.dependencies) or "None"
    review_notes = "\n".join(f"- {note}" for note in task.review_notes)
    human_guidance = (
        f"\nHuman approval instructions:\n- {human_instructions}\n"
        if human_instructions
        else ""
    )

    return f"""Follow the assigned operating procedure for this implementation stage.

Plan / spec context:
{plan_output[:3000]}

Original user request:
{flow.state.request}

Target repo: {flow.state.target_repo}

Current revision count: {flow.state.revisions}

Task:
- Id: {task.id}
- Title: {task.title or task.description}
- Description: {task.description}
- Current task status: {task.status}
- Dependencies satisfied: {dependencies}

Acceptance criteria:
{acceptance or "- None provided"}

Verification:
{verification or "- None provided"}

Files likely touched:
{files or "- None provided"}

Review notes for this task:
{review_notes or "- None provided"}
{human_guidance}

Work only on this task unless an additional file change is required to keep the repo coherent.
After you are done, summarize what changed and whether task verification now passes.
""".strip()


def track_changed_files(flow, changed_files: list[str]) -> None:
    seen = set(flow.state.changed_files)
    for path in changed_files:
        normalized = path.strip()
        if not normalized or normalized in seen:
            continue
        flow.state.changed_files.append(normalized)
        seen.add(normalized)


def next_task_attempt(flow, task_id: int) -> int:
    attempts = 0
    for entry in flow.state.task_executions:
        candidate = (
            entry
            if isinstance(entry, TaskExecutionEntry)
            else TaskExecutionEntry.model_validate(entry)
        )
        if candidate.task_id == task_id:
            attempts += 1
    return attempts + 1


def record_task_execution(
    flow,
    *,
    task: TaskItem,
    stage_cfg,
    cwd: str,
    result: CoderResult,
    changed_files: list[str],
    crew_rounds: list[CrewRoundEntry],
    parallel_batch_id: str | None,
) -> None:
    target_repo = str(Path(flow.state.target_repo).resolve(strict=False))
    resolved_cwd = str(Path(cwd).resolve(strict=False))
    isolated_workspace = resolved_cwd != target_repo
    flow.state.task_executions.append(
        TaskExecutionEntry(
            task_id=task.id,
            attempt=flow._next_task_attempt(task.id),
            revision=flow.state.revisions,
            worker=stage_cfg.worker,
            model=stage_cfg.model,
            orchestration=(
                "crew" if flow._do_work_crew_enabled(stage_cfg) else "direct"
            ),
            success=result.success,
            summary=result.summary or result.raw_output,
            error=result.error,
            changed_files=changed_files,
            isolated_workspace=isolated_workspace,
            workspace=resolved_cwd if isolated_workspace else None,
            parallel_batch_id=parallel_batch_id,
            crew_rounds=crew_rounds,
        )
    )
    flow._refresh_debug_report()


def run_task_with_change_tracking(
    flow,
    *,
    worker_tool: HeadlessCoderTool,
    task: TaskItem,
    cwd: str,
    stage_cfg,
    plan_output: str,
    human_instructions: str | None = None,
    parallel_batch_id: str | None = None,
):
    before = snapshot_workspace(Path(cwd))
    task_prompt = flow._build_task_execution_prompt(
        task,
        plan_output,
        human_instructions=human_instructions,
    )
    crew_rounds: list[CrewRoundEntry] = []
    if flow._do_work_crew_enabled(stage_cfg):
        crew_cfg = stage_cfg.extra.get("crew", {}) or {}
        try:
            result, decision = run_do_work_crew(
                task_prompt=task_prompt,
                worker_tool=worker_tool,
                cwd=cwd,
                timeout=stage_cfg.timeout,
                model=stage_cfg.model,
                crew_config=crew_cfg,
                round_observer=lambda event: crew_rounds.append(
                    CrewRoundEntry.model_validate(event)
                ),
                config_dir=flow.state.config_dir,
            )
        except Exception as exc:
            result = CoderResult(
                summary="",
                raw_output="",
                exit_code=1,
                error=f"Implementation Crew failed: {exc}",
            )
        else:
            if result.success and decision.status != "pass":
                issues = "; ".join(decision.issues) or decision.summary
                result = CoderResult(
                    summary=decision.summary or result.summary,
                    changed_files=result.changed_files,
                    tests_passed=result.tests_passed,
                    raw_output=result.raw_output,
                    exit_code=1,
                    error=f"Implementation Crew requested revision: {issues}",
                )
            elif result.success and decision.summary.strip():
                result = CoderResult(
                    summary=decision.summary,
                    changed_files=result.changed_files,
                    tests_passed=result.tests_passed,
                    raw_output=result.raw_output,
                    exit_code=result.exit_code,
                    error=result.error,
                )
    else:
        result = worker_tool.run(
            task=task_prompt,
            cwd=cwd,
            mode="edit",
            timeout=stage_cfg.timeout,
            model=stage_cfg.model,
        )
    after = snapshot_workspace(Path(cwd))
    detected = diff_workspace_snapshots(before, after)
    changed_files = sorted(set(result.changed_files) | set(detected))
    created_files = sorted(set(after) - set(before))
    flow._record_task_execution(
        task=task,
        stage_cfg=stage_cfg,
        cwd=cwd,
        result=result,
        changed_files=changed_files,
        crew_rounds=crew_rounds,
        parallel_batch_id=parallel_batch_id,
    )
    return result, changed_files, created_files


def deny_patterns(flow) -> list[str]:
    return list(flow.config.paths.get("deny") or [])


def denied_failure_result(
    flow, denied: dict[str, str], unrestorable: list[str]
) -> CoderResult:
    details = ", ".join(
        f"{path} (matched {pattern!r})" for path, pattern in sorted(denied.items())
    )
    error = f"Denied paths touched: {details}."
    if unrestorable:
        error += " Could not restore: " + ", ".join(sorted(unrestorable)) + "."
    return CoderResult(summary="", raw_output="", exit_code=1, error=error)


def run_serial_task(
    flow,
    *,
    worker_tool: HeadlessCoderTool,
    task: TaskItem,
    stage_cfg,
    plan_output: str,
    human_instructions: str | None,
) -> tuple[CoderResult, list[str]]:
    """Run one structured task serially, enforcing paths.deny.

    ``do_work.isolation: copy`` runs the task in a disposable workspace
    copy and merges only clean results back — denied or unsafe paths
    never reach the real repo, and a failed task leaves it pristine.
    The default ``in_place`` keeps today's behavior with post-hoc
    restore of denied paths.
    """
    deny = flow._deny_patterns()
    isolation = stage_cfg.extra.get("isolation", "in_place")

    if isolation != "copy":
        result, changed_files, created_files = flow._run_task_with_change_tracking(
            worker_tool=worker_tool,
            task=task,
            cwd=flow.state.target_repo,
            stage_cfg=stage_cfg,
            plan_output=plan_output,
            human_instructions=human_instructions,
            parallel_batch_id=None,
        )
        denied = match_denied(changed_files, deny)
        if not denied:
            return result, changed_files
        unrestorable = restore_denied_paths(
            flow.state.target_repo,
            sorted(denied),
            created=created_files,
        )
        remaining = [
            path for path in changed_files if path not in denied or path in unrestorable
        ]
        return flow._denied_failure_result(denied, unrestorable), remaining

    try:
        workspace = create_workspace_copy(
            Path(flow.state.target_repo),
            prefix=f"flow-serial-task-{task.id}-",
        )
    except OSError as exc:
        # A copy failure (disk full, permissions) fails this task, not
        # the whole run; the target repo is untouched.
        return (
            CoderResult(
                summary="",
                raw_output="",
                exit_code=1,
                error=f"Could not create isolated workspace copy: {exc}",
            ),
            [],
        )
    try:
        result, changed_files, _created = flow._run_task_with_change_tracking(
            worker_tool=worker_tool,
            task=task,
            cwd=str(workspace),
            stage_cfg=stage_cfg,
            plan_output=plan_output,
            human_instructions=human_instructions,
            parallel_batch_id=None,
        )
        if not result.success:
            # Nothing merged: the target repo is untouched.
            return result, []
        denied = match_denied(changed_files, deny)
        if denied:
            return flow._denied_failure_result(denied, []), []
        try:
            apply_changed_files(
                src_root=workspace,
                dest_root=Path(flow.state.target_repo),
                changed_files=changed_files,
            )
        except ValueError as exc:
            return (
                CoderResult(
                    summary="",
                    raw_output="",
                    exit_code=1,
                    error=f"Mergeback rejected unsafe path: {exc}",
                ),
                [],
            )
        return result, changed_files
    finally:
        cleanup_workspace_copy(workspace)


def run_unstructured_edit(
    flow,
    worker_tool: HeadlessCoderTool,
    stage_cfg,
    prompt: str,
) -> tuple[CoderResult, list[str]]:
    """Run the direct (task-less) edit, enforcing paths.deny.

    Snapshot-brackets the run so change detection no longer relies on
    the worker's flow-report (mirrors finalize). Honors
    ``do_work.isolation: copy`` like the structured serial path.
    """
    deny = flow._deny_patterns()
    isolation = stage_cfg.extra.get("isolation", "in_place")
    target = Path(flow.state.target_repo)

    if isolation == "copy":
        try:
            workspace = create_workspace_copy(target, prefix="flow-direct-edit-")
        except OSError as exc:
            error = f"Could not create isolated workspace copy: {exc}"
            flow.state.errors.append(error)
            return (
                CoderResult(summary="", raw_output="", exit_code=1, error=error),
                [],
            )
        try:
            before = snapshot_workspace(workspace)
            result = worker_tool.run(
                task=prompt,
                cwd=str(workspace),
                mode="edit",
                timeout=stage_cfg.timeout,
                model=stage_cfg.model,
            )
            after = snapshot_workspace(workspace)
            changed = sorted(
                set(result.changed_files) | set(diff_workspace_snapshots(before, after))
            )
            if not result.success:
                # Nothing merged: the target repo is untouched.
                return result, []
            denied = match_denied(changed, deny)
            if denied:
                failure = flow._denied_failure_result(denied, [])
                flow.state.errors.append(failure.error or "")
                return failure, []
            try:
                apply_changed_files(
                    src_root=workspace,
                    dest_root=target,
                    changed_files=changed,
                )
            except ValueError as exc:
                error = f"Mergeback rejected unsafe path: {exc}"
                flow.state.errors.append(error)
                return (
                    CoderResult(summary="", raw_output="", exit_code=1, error=error),
                    [],
                )
            return result, changed
        finally:
            cleanup_workspace_copy(workspace)

    before = snapshot_workspace(target)
    result = worker_tool.run(
        task=prompt,
        cwd=flow.state.target_repo,
        mode="edit",
        timeout=stage_cfg.timeout,
        model=stage_cfg.model,
    )
    after = snapshot_workspace(target)
    changed = sorted(
        set(result.changed_files) | set(diff_workspace_snapshots(before, after))
    )
    denied = match_denied(changed, deny)
    if not denied:
        return result, changed
    created = sorted(set(after) - set(before))
    unrestorable = restore_denied_paths(target, sorted(denied), created=created)
    failure = flow._denied_failure_result(denied, unrestorable)
    flow.state.errors.append(failure.error or "")
    remaining = [path for path in changed if path not in denied or path in unrestorable]
    return failure, remaining


def mark_task_complete(
    flow,
    task: TaskItem,
    *,
    summary: str,
    changed_files: list[str],
) -> None:
    task.status = "done"
    task.review_notes = []
    task.last_error = None
    flow._track_changed_files(changed_files)
    flow._record_history(
        kind="task_complete",
        summary=f"Task {task.id} completed.",
        task_ids=[task.id],
        files=changed_files or task.files,
        details=[summary],
    )


def mark_task_failed(
    flow,
    task: TaskItem,
    *,
    error: str,
    changed_files: list[str],
) -> None:
    task.status = "failed"
    task.last_error = error
    flow._record_history(
        kind="task_failed",
        summary=f"Task {task.id} failed.",
        task_ids=[task.id],
        files=changed_files or task.files,
        details=[error],
    )


def parallel_conflicts(
    flow, outcomes: list[ParallelTaskOutcome]
) -> dict[int, list[str]]:
    path_to_tasks: dict[str, set[int]] = {}
    for outcome in outcomes:
        if not outcome.success:
            continue
        for path in outcome.changed_files:
            path_to_tasks.setdefault(path, set()).add(outcome.task.id)

    conflicts: dict[int, set[str]] = {}
    for path, task_ids in path_to_tasks.items():
        if len(task_ids) < 2:
            continue
        for task_id in task_ids:
            conflicts.setdefault(task_id, set()).add(path)

    return {task_id: sorted(paths) for task_id, paths in conflicts.items()}


def parallel_batch_planner_enabled(flow, stage_cfg) -> bool:
    parallel_cfg = stage_cfg.extra.get("parallel", {}) or {}
    planner_cfg = parallel_cfg.get("planner", {}) or {}
    return flow._parallel_do_work_enabled(stage_cfg) and bool(
        planner_cfg.get("enabled", False)
    )


def revision_replanner_enabled(flow) -> bool:
    do_work_cfg = flow.config.get_stage("do_work")
    replan_cfg = do_work_cfg.extra.get("replan", {}) or {}
    return bool(replan_cfg.get("enabled", False))


def execution_replanner_enabled(flow) -> bool:
    do_work_cfg = flow.config.get_stage("do_work")
    replan_cfg = do_work_cfg.extra.get("replan", {}) or {}
    return bool(replan_cfg.get("enabled", False)) and bool(
        replan_cfg.get("on_execution_failure", False)
    )


def cross_task_success_replanner_enabled(flow) -> bool:
    do_work_cfg = flow.config.get_stage("do_work")
    replan_cfg = do_work_cfg.extra.get("replan", {}) or {}
    return bool(replan_cfg.get("enabled", False)) and bool(
        replan_cfg.get("on_cross_task_change", False)
    )


def ambiguous_success_replanner_enabled(flow) -> bool:
    do_work_cfg = flow.config.get_stage("do_work")
    replan_cfg = do_work_cfg.extra.get("replan", {}) or {}
    return bool(replan_cfg.get("enabled", False)) and bool(
        replan_cfg.get("on_ambiguous_success", False)
    )


def max_execution_replans(flow) -> int:
    do_work_cfg = flow.config.get_stage("do_work")
    replan_cfg = do_work_cfg.extra.get("replan", {}) or {}
    value = int(replan_cfg.get("max_execution_replans", 1))
    if value < 1:
        raise ValueError("do_work.replan.max_execution_replans must be at least 1")
    return value


def execution_replanning_count(flow) -> int:
    return sum(
        1
        for entry in flow._normalized_history_entries()
        if entry.kind == "execution_replanning"
    )


def planning_tool_for_do_work(
    flow, worker_tool: HeadlessCoderTool
) -> HeadlessCoderTool:
    planning_skill = flow.config.skills.get("plan")
    base_worker = getattr(worker_tool, "worker", worker_tool)
    loader = getattr(worker_tool, "loader", flow.loader)
    return HeadlessCoderTool(
        worker=base_worker,
        skill_name=planning_skill,
        loader=loader,
    )


def build_batch_planning_prompt(
    flow,
    *,
    ready_tasks: list[TaskItem],
    max_workers: int,
    plan_output: str,
    human_instructions: str | None = None,
) -> str:
    task_lines: list[str] = []
    for task in ready_tasks:
        files = ", ".join(task.files) if task.files else "unknown"
        notes = "; ".join(task.review_notes) if task.review_notes else "none"
        task_lines.append(
            f"- Task {task.id}: {task.title or task.description} | "
            f"files={files} | review_notes={notes}"
        )
    human_guidance = (
        f"\nHuman approval instructions:\n- {human_instructions}\n"
        if human_instructions
        else ""
    )
    return f"""You are selecting the next execution batch for structured do_work.

Original request:
{flow.state.request}

Plan/spec context:
{plan_output[:3000]}

Ready tasks:
{chr(10).join(task_lines)}
{human_guidance}

Goal:
- Choose up to {max_workers} ready tasks for next execution batch.
- Prefer the largest batch likely safe to run in parallel in isolated workspaces.
- If uncertainty is high, return one task only.
- You may add likely file hints for selected tasks when current file lists look missing or weak.
- Never select tasks outside ready list.

Return only structured JSON matching schema.
""".strip()


def collect_batch_file_hints(
    flow,
    *,
    ready_by_id: dict[int, TaskItem],
    planned_tasks,
) -> dict[int, list[str]]:
    hinted_files: dict[int, list[str]] = {}
    for planned_task in planned_tasks:
        task = ready_by_id.get(planned_task.task_id)
        if task is None:
            continue
        merged_files = list(task.files)
        existing = {path.strip() for path in task.files if path.strip()}
        for path in planned_task.files:
            normalized = path.strip()
            if normalized and normalized not in existing:
                merged_files.append(normalized)
                existing.add(normalized)
        hinted_files[task.id] = merged_files
    return hinted_files


def planned_batch_preview(
    flow,
    *,
    ready_by_id: dict[int, TaskItem],
    task_ids: list[int],
    hinted_files: dict[int, list[str]],
) -> list[TaskItem]:
    preview: list[TaskItem] = []
    for task_id in task_ids:
        task = ready_by_id[task_id].model_copy(deep=True)
        task.files = list(hinted_files.get(task_id, task.files))
        preview.append(task)
    return preview


def ensure_conservative_planned_batch(flow, batch: list[TaskItem]) -> None:
    if len(batch) < 2:
        return

    seen_files: set[str] = set()
    for task in batch:
        task_files = {path.strip() for path in task.files if path.strip()}
        if not task_files:
            raise ValueError(
                "do_work batch planner returned a weak plan: "
                f"task {task.id} still has no file hints"
            )
        overlap = task_files & seen_files
        if overlap:
            overlap_text = ", ".join(sorted(overlap))
            raise ValueError(
                "do_work batch planner returned a weak plan: "
                f"selected tasks still overlap on {overlap_text}"
            )
        seen_files |= task_files


def planned_execution_batch(
    flow,
    *,
    worker_tool: HeadlessCoderTool,
    stage_cfg,
    plan_output: str,
    max_workers: int,
    human_instructions: str | None = None,
    execution_target_task_ids: list[int] | None = None,
) -> PlannedBatchSelection:
    allowed_task_ids = set(execution_target_task_ids or []) or None
    ready = ready_execution_tasks(
        flow.state.tasks,
        allowed_task_ids=allowed_task_ids,
    )
    if len(ready) < 2:
        return PlannedBatchSelection(
            task_ids=[],
            hinted_files={},
            summary="",
            planned_files=[],
        )

    planner_tool = flow._planning_tool_for_do_work(worker_tool)
    prompt = flow._build_batch_planning_prompt(
        ready_tasks=ready,
        max_workers=max_workers,
        plan_output=plan_output,
        human_instructions=human_instructions,
    )
    result = planner_tool.run(
        task=prompt,
        cwd=flow.state.target_repo,
        mode="inspect",
        schema=DO_WORK_BATCH_PLAN_SCHEMA,
        timeout=stage_cfg.timeout,
        model=stage_cfg.model,
    )
    plan = normalize_do_work_batch_plan([result.summary, result.raw_output])
    if plan is None:
        raise ValueError("do_work batch planner output could not be parsed")

    ready_by_id = {task.id: task for task in ready}
    allowed_task_ids = set(ready_by_id)
    plan = require_concrete_do_work_batch_plan(
        plan,
        allowed_task_ids=allowed_task_ids,
        max_workers=max_workers,
    )
    task_ids = [planned_task.task_id for planned_task in plan.tasks]
    hinted_files = flow._collect_batch_file_hints(
        ready_by_id=ready_by_id,
        planned_tasks=plan.tasks,
    )
    flow._ensure_conservative_planned_batch(
        flow._planned_batch_preview(
            ready_by_id=ready_by_id,
            task_ids=task_ids,
            hinted_files=hinted_files,
        )
    )
    return PlannedBatchSelection(
        task_ids=task_ids,
        hinted_files=hinted_files,
        summary=plan.summary,
        planned_files=sorted(
            {
                path
                for planned_task in plan.tasks
                for path in planned_task.files
                if path.strip()
            }
        ),
    )


def revision_planning_worker_and_stage(
    flow, do_work_worker: HeadlessCoderTool
) -> tuple[HeadlessCoderTool, Any]:
    if "plan" in flow.config.skills and "plan" in flow._workers:
        return flow._get_worker("plan"), flow.config.get_stage("plan")
    return (
        flow._planning_tool_for_do_work(do_work_worker),
        flow.config.get_stage("do_work"),
    )


def build_revision_replan_prompt(
    flow,
    *,
    target_ids: set[int],
    downstream_ids: set[int],
) -> str:
    current_plan = render_plan_markdown(
        state_items_to_plan_output(flow.state.spec, flow.state.tasks)
    )
    task_state_lines: list[str] = []
    for task in flow.state.tasks:
        files = ", ".join(task.files) if task.files else "n/a"
        review_notes = "; ".join(task.review_notes) if task.review_notes else "none"
        dependencies = ", ".join(str(dep) for dep in task.dependencies) or "none"
        task_state_lines.append(
            f"- Task {task.id}: status={task.status} | deps={dependencies} | "
            f"files={files} | notes={review_notes}"
        )
    hint_lines: list[str] = []
    for hint in flow._normalized_review_task_hints():
        hint_lines.append(
            f"- tasks={','.join(str(task_id) for task_id in hint.task_ids) or 'none'} "
            f"| files={','.join(hint.files) or 'none'} | {hint.summary}"
        )
    issues_text = "\n".join(f"- {issue}" for issue in flow.state.issues) or "- None"
    human_replan_reason = flow.state.pending_revision_replan_reason
    human_replan_text = (
        f"\nHuman-requested replanning guidance:\n- {human_replan_reason}\n"
        if human_replan_reason
        else ""
    )
    return f"""You are replanning the structured task graph after a failed review round.

Original request:
{flow.state.request}

Current spec and task graph:
{current_plan}

Current execution state:
{chr(10).join(task_state_lines)}

Review issues:
{issues_text}

Review task hints:
{chr(10).join(hint_lines) or "- None"}
{human_replan_text}

Directly targeted task ids: {sorted(target_ids)}
Downstream task ids that may need rework: {sorted(downstream_ids)}

Recent history:
{flow._history_summary(limit=8)}

Rules:
- Return a full revised structured plan object, not a partial patch.
- Preserve unchanged valid completed tasks with same ids whenever practical.
- You may split, merge, reorder, or add tasks when review evidence suggests current graph is wrong.
- Keep ids stable for unchanged tasks when possible.
- Update dependencies, files, acceptance criteria, and verification so remaining work is executable.
- Keep scope limited to satisfying current review issues.
""".strip()


def build_execution_replan_prompt(
    flow,
    *,
    failed_task: TaskItem,
    error: str,
    changed_files: list[str],
    target_ids: set[int],
    downstream_ids: set[int],
) -> str:
    current_plan = render_plan_markdown(
        state_items_to_plan_output(flow.state.spec, flow.state.tasks)
    )
    task_state_lines: list[str] = []
    for task in flow.state.tasks:
        files = ", ".join(task.files) if task.files else "n/a"
        review_notes = "; ".join(task.review_notes) if task.review_notes else "none"
        dependencies = ", ".join(str(dep) for dep in task.dependencies) or "none"
        task_state_lines.append(
            f"- Task {task.id}: status={task.status} | deps={dependencies} | "
            f"files={files} | notes={review_notes}"
        )
    changed = ", ".join(changed_files) if changed_files else "none"
    return f"""You are replanning the structured task graph after a task failed during do_work.

Original request:
{flow.state.request}

Current spec and task graph:
{current_plan}

Current execution state:
{chr(10).join(task_state_lines)}

Runtime failure:
- Task id: {failed_task.id}
- Task title: {failed_task.title or failed_task.description}
- Error: {error}
- Changed files observed during failed attempt: {changed}

Directly targeted task ids: {sorted(target_ids)}
Downstream task ids that may need rework: {sorted(downstream_ids)}

Recent history:
{flow._history_summary(limit=8)}

Rules:
- Return a full revised structured plan object, not a partial patch.
- Preserve unchanged valid completed tasks with same ids whenever practical.
- You may split, merge, reorder, or add remaining tasks to recover from this failure.
- Keep ids stable for unchanged tasks when possible.
- Update dependencies, files, acceptance criteria, and verification so remaining work is executable.
- Focus on recovering from runtime failure inside current do_work round.
""".strip()


def cross_task_success_targets(
    flow,
    *,
    source_task: TaskItem,
    changed_files: list[str],
) -> tuple[set[int], list[str]]:
    changed = {path.strip() for path in changed_files if path.strip()}
    if not changed:
        return set(), []

    target_ids: set[int] = set()
    overlapping_files: set[str] = set()

    for task in flow.state.tasks:
        if task.id == source_task.id or task.status == "done":
            continue
        task_files = {path.strip() for path in task.files if path.strip()}
        overlap = changed & task_files
        if overlap:
            target_ids.add(task.id)
            overlapping_files |= overlap

    return target_ids, sorted(overlapping_files)


def build_cross_task_success_replan_prompt(
    flow,
    *,
    source_task: TaskItem,
    changed_files: list[str],
    target_ids: set[int],
    overlapping_files: list[str],
    downstream_ids: set[int],
) -> str:
    current_plan = render_plan_markdown(
        state_items_to_plan_output(flow.state.spec, flow.state.tasks)
    )
    changed = {path.strip() for path in changed_files if path.strip()}
    task_state_lines: list[str] = []
    impacted_lines: list[str] = []

    for task in flow.state.tasks:
        files = ", ".join(task.files) if task.files else "n/a"
        review_notes = "; ".join(task.review_notes) if task.review_notes else "none"
        dependencies = ", ".join(str(dep) for dep in task.dependencies) or "none"
        task_state_lines.append(
            f"- Task {task.id}: status={task.status} | deps={dependencies} | "
            f"files={files} | notes={review_notes}"
        )
        if task.id in target_ids:
            overlap = sorted(
                {path.strip() for path in task.files if path.strip()} & changed
            )
            impacted_lines.append(
                f"- Task {task.id}: status={task.status} | overlap={', '.join(overlap) or 'none'} "
                f"| files={files}"
            )

    return f"""You are replanning the structured task graph after a successful do_work task changed files assigned to other remaining tasks.

Original request:
{flow.state.request}

Current spec and task graph:
{current_plan}

Current execution state:
{chr(10).join(task_state_lines)}

Successful task evidence:
- Task id: {source_task.id}
- Task title: {source_task.title or source_task.description}
- Changed files observed: {", ".join(changed_files) or "none"}
- Overlapping planned files for remaining tasks: {", ".join(overlapping_files) or "none"}

Impacted remaining tasks:
{chr(10).join(impacted_lines) or "- None"}

Directly targeted task ids: {sorted(target_ids)}
Downstream task ids that may need rework: {sorted(downstream_ids)}

Recent history:
{flow._history_summary(limit=8)}

Rules:
- Return a full revised structured plan object, not a partial patch.
- Preserve unchanged valid completed tasks with same ids whenever practical.
- You may split, merge, reorder, or add remaining tasks when this success evidence shows current task boundaries are stale.
- Keep ids stable for unchanged tasks when possible.
- Update dependencies, files, acceptance criteria, and verification so remaining work is executable.
- Focus only on work made stale or newly clarified by the observed cross-task file changes.
""".strip()


def ambiguous_success_reasons(
    flow, *, task: TaskItem, changed_files: list[str]
) -> list[str]:
    changed = {path.strip() for path in changed_files if path.strip()}
    planned = {path.strip() for path in task.files if path.strip()}
    reasons: list[str] = []

    if not changed:
        reasons.append("Task exited successfully but no files changed.")
    if changed and planned and not (changed & planned):
        reasons.append(
            "Task exited successfully but none of its planned files were changed."
        )

    return reasons


def build_ambiguous_success_replan_prompt(
    flow,
    *,
    source_task: TaskItem,
    changed_files: list[str],
    reasons: list[str],
    target_ids: set[int],
    downstream_ids: set[int],
) -> str:
    current_plan = render_plan_markdown(
        state_items_to_plan_output(flow.state.spec, flow.state.tasks)
    )
    task_state_lines: list[str] = []
    for task in flow.state.tasks:
        files = ", ".join(task.files) if task.files else "n/a"
        review_notes = "; ".join(task.review_notes) if task.review_notes else "none"
        dependencies = ", ".join(str(dep) for dep in task.dependencies) or "none"
        task_state_lines.append(
            f"- Task {task.id}: status={task.status} | deps={dependencies} | "
            f"files={files} | notes={review_notes}"
        )

    planned = ", ".join(source_task.files) if source_task.files else "none"
    changed = ", ".join(changed_files) if changed_files else "none"
    reason_lines = "\n".join(f"- {reason}" for reason in reasons) or "- None"
    return f"""You are replanning the structured task graph after a do_work task reported success with ambiguous execution evidence.

Original request:
{flow.state.request}

Current spec and task graph:
{current_plan}

Current execution state:
{chr(10).join(task_state_lines)}

Ambiguous success evidence:
- Task id: {source_task.id}
- Task title: {source_task.title or source_task.description}
- Planned task files: {planned}
- Changed files observed: {changed}
- Evidence concerns:
{reason_lines}

Directly targeted task ids: {sorted(target_ids)}
Downstream task ids that may need rework: {sorted(downstream_ids)}

Recent history:
{flow._history_summary(limit=8)}

Rules:
- Return a full revised structured plan object, not a partial patch.
- Preserve unchanged valid completed tasks with same ids whenever practical.
- You may split, merge, reorder, or add remaining tasks when this weak success evidence suggests the current graph is stale or unfinished.
- Keep ids stable for unchanged tasks when possible.
- Update dependencies, files, acceptance criteria, and verification so remaining work is executable.
- Focus on recovering from the ambiguous success evidence inside the current do_work round.
""".strip()


def task_definition_matches(flow, current: TaskItem, replanned: TaskItem) -> bool:
    return (
        current.title == replanned.title
        and current.description == replanned.description
        and current.acceptance_criteria == replanned.acceptance_criteria
        and current.verification == replanned.verification
        and current.dependencies == replanned.dependencies
        and current.files == replanned.files
        and current.estimated_scope == replanned.estimated_scope
    )


def apply_replanned_tasks(
    flow,
    *,
    plan: PlanOutput,
    target_ids: set[int],
    downstream_ids: set[int],
) -> None:
    existing_by_id = {task.id: task for task in flow.state.tasks}
    notes_by_task = flow._build_revision_notes_by_task(target_ids)
    reset_ids = target_ids | downstream_ids
    replanned_tasks = plan_tasks_to_state_items(plan.tasks)

    for task in replanned_tasks:
        previous = existing_by_id.get(task.id)
        if (
            previous is not None
            and task.id not in reset_ids
            and flow._task_definition_matches(previous, task)
        ):
            task.status = previous.status
            task.review_notes = previous.review_notes.copy()
            task.last_error = previous.last_error
            continue

        if task.id in target_ids:
            task.status = "needs_revision"
            task.review_notes = notes_by_task.get(task.id, flow.state.issues.copy())
            task.last_error = None
            continue

        if task.id in downstream_ids:
            task.status = "pending"
            task.review_notes = ["Upstream dependency requires revision."]
            task.last_error = None
            continue

        if (
            previous is not None
            and previous.status != "done"
            and flow._task_definition_matches(previous, task)
        ):
            task.status = previous.status
            task.review_notes = previous.review_notes.copy()
            task.last_error = previous.last_error
        else:
            task.status = "pending"
            task.review_notes = []
            task.last_error = None

    flow.state.spec = plan.spec
    flow.state.tasks = replanned_tasks
    flow.state.review_task_hints = []


def attempt_structured_revision_replan(flow, do_work_worker: HeadlessCoderTool) -> bool:
    human_requested = flow._revision_replan_requested()
    if (not flow._revision_replanner_enabled() and not human_requested) or (
        not flow.state.tasks
    ):
        return False

    target_ids = flow._collect_target_task_ids()
    downstream_ids = flow._expand_dependent_task_ids(target_ids)
    planning_worker, stage_cfg = flow._revision_planning_worker_and_stage(
        do_work_worker
    )
    prompt = flow._build_revision_replan_prompt(
        target_ids=target_ids,
        downstream_ids=downstream_ids,
    )
    try:
        result = planning_worker.run(
            task=prompt,
            cwd=flow.state.target_repo,
            mode="inspect",
            schema=PlanOutput.model_json_schema(),
            timeout=stage_cfg.timeout,
            model=stage_cfg.model,
        )
        plan = require_concrete_plan(
            normalize_plan_output([result.raw_output, result.summary])
        )
    except Exception:
        flow._clear_pending_revision_replan()
        return False

    human_reason = flow.state.pending_revision_replan_reason
    flow._clear_pending_revision_replan()
    flow._apply_replanned_tasks(
        plan=plan,
        target_ids=target_ids,
        downstream_ids=downstream_ids,
    )
    flow._record_history(
        kind="revision_replanning",
        summary=(
            "Replanned structured task graph from human review request."
            if human_requested
            else "Replanned structured task graph."
        ),
        task_ids=[task.id for task in flow.state.tasks if task.status != "done"],
        files=sorted(
            {path for task in flow.state.tasks for path in task.files if path.strip()}
        ),
        details=[
            *([human_reason] if human_requested and human_reason else []),
            plan.spec,
        ],
    )
    return True


def attempt_structured_execution_replan(
    flow,
    *,
    do_work_worker: HeadlessCoderTool,
    failed_task: TaskItem,
    error: str,
    changed_files: list[str],
) -> bool:
    if (
        not flow._execution_replanner_enabled()
        or not flow.state.tasks
        or flow._execution_replanning_count() >= flow._max_execution_replans()
    ):
        return False

    target_ids = {failed_task.id}
    downstream_ids = flow._expand_dependent_task_ids(target_ids)
    planning_worker, stage_cfg = flow._revision_planning_worker_and_stage(
        do_work_worker
    )
    prompt = flow._build_execution_replan_prompt(
        failed_task=failed_task,
        error=error,
        changed_files=changed_files,
        target_ids=target_ids,
        downstream_ids=downstream_ids,
    )
    try:
        result = planning_worker.run(
            task=prompt,
            cwd=flow.state.target_repo,
            mode="inspect",
            schema=PlanOutput.model_json_schema(),
            timeout=stage_cfg.timeout,
            model=stage_cfg.model,
        )
        plan = require_concrete_plan(
            normalize_plan_output([result.raw_output, result.summary])
        )
    except Exception:
        return False

    flow._apply_replanned_tasks(
        plan=plan,
        target_ids=target_ids,
        downstream_ids=downstream_ids,
    )
    flow._record_history(
        kind="execution_replanning",
        summary=(
            f"Replanned structured task graph after task {failed_task.id} "
            "failed during do_work."
        ),
        task_ids=[task.id for task in flow.state.tasks if task.status != "done"],
        files=sorted(
            {path for task in flow.state.tasks for path in task.files if path.strip()}
        ),
        details=[error, plan.spec],
    )
    return True


def attempt_structured_cross_task_success_replan(
    flow,
    *,
    do_work_worker: HeadlessCoderTool,
    source_task: TaskItem,
    changed_files: list[str],
) -> bool:
    if (
        not flow._cross_task_success_replanner_enabled()
        or not flow.state.tasks
        or flow._execution_replanning_count() >= flow._max_execution_replans()
    ):
        return False

    target_ids, overlapping_files = flow._cross_task_success_targets(
        source_task=source_task,
        changed_files=changed_files,
    )
    if not target_ids:
        return False

    downstream_ids = flow._expand_dependent_task_ids(target_ids)
    planning_worker, stage_cfg = flow._revision_planning_worker_and_stage(
        do_work_worker
    )
    prompt = flow._build_cross_task_success_replan_prompt(
        source_task=source_task,
        changed_files=changed_files,
        target_ids=target_ids,
        overlapping_files=overlapping_files,
        downstream_ids=downstream_ids,
    )
    try:
        result = planning_worker.run(
            task=prompt,
            cwd=flow.state.target_repo,
            mode="inspect",
            schema=PlanOutput.model_json_schema(),
            timeout=stage_cfg.timeout,
            model=stage_cfg.model,
        )
        plan = require_concrete_plan(
            normalize_plan_output([result.raw_output, result.summary])
        )
    except Exception:
        return False

    flow._apply_replanned_tasks(
        plan=plan,
        target_ids=target_ids,
        downstream_ids=downstream_ids,
    )
    flow._record_history(
        kind="execution_replanning",
        summary=(
            f"Replanned structured task graph after task {source_task.id} "
            "changed files assigned to other tasks during do_work."
        ),
        task_ids=[task.id for task in flow.state.tasks if task.status != "done"],
        files=sorted(
            {path for task in flow.state.tasks for path in task.files if path.strip()}
        ),
        details=[
            f"Changed files: {', '.join(changed_files) or 'none'}",
            f"Overlapping planned files: {', '.join(overlapping_files) or 'none'}",
            plan.spec,
        ],
    )
    return True


def attempt_structured_ambiguous_success_replan(
    flow,
    *,
    do_work_worker: HeadlessCoderTool,
    source_task: TaskItem,
    changed_files: list[str],
) -> bool:
    if (
        not flow._ambiguous_success_replanner_enabled()
        or not flow.state.tasks
        or flow._execution_replanning_count() >= flow._max_execution_replans()
    ):
        return False

    reasons = flow._ambiguous_success_reasons(
        task=source_task,
        changed_files=changed_files,
    )
    if not reasons:
        return False

    target_ids = {source_task.id}
    downstream_ids = flow._expand_dependent_task_ids(target_ids)
    planning_worker, stage_cfg = flow._revision_planning_worker_and_stage(
        do_work_worker
    )
    prompt = flow._build_ambiguous_success_replan_prompt(
        source_task=source_task,
        changed_files=changed_files,
        reasons=reasons,
        target_ids=target_ids,
        downstream_ids=downstream_ids,
    )
    try:
        result = planning_worker.run(
            task=prompt,
            cwd=flow.state.target_repo,
            mode="inspect",
            schema=PlanOutput.model_json_schema(),
            timeout=stage_cfg.timeout,
            model=stage_cfg.model,
        )
        plan = require_concrete_plan(
            normalize_plan_output([result.raw_output, result.summary])
        )
    except Exception:
        return False

    flow._apply_replanned_tasks(
        plan=plan,
        target_ids=target_ids,
        downstream_ids=downstream_ids,
    )
    flow._record_history(
        kind="execution_replanning",
        summary=(
            f"Replanned structured task graph after task {source_task.id} "
            "reported ambiguous success during do_work."
        ),
        task_ids=[task.id for task in flow.state.tasks if task.status != "done"],
        files=sorted(
            {path for task in flow.state.tasks for path in task.files if path.strip()}
        ),
        details=[
            *reasons,
            f"Changed files: {', '.join(changed_files) or 'none'}",
            plan.spec,
        ],
    )
    return True


def select_structured_execution_batch(
    flow,
    *,
    worker_tool: HeadlessCoderTool,
    stage_cfg,
    plan_output: str,
    max_workers: int,
    human_instructions: str | None = None,
    execution_target_task_ids: list[int] | None = None,
) -> list[TaskItem]:
    allowed_task_ids = set(execution_target_task_ids or []) or None
    static_batch = select_execution_batch(
        flow.state.tasks,
        max_workers,
        allowed_task_ids=allowed_task_ids,
    )
    if not flow._parallel_batch_planner_enabled(stage_cfg):
        return static_batch

    ready = ready_execution_tasks(
        flow.state.tasks,
        allowed_task_ids=allowed_task_ids,
    )
    if len(ready) <= len(static_batch) or len(static_batch) >= max(1, max_workers):
        return static_batch

    ready_by_id = {task.id: task for task in ready}
    try:
        planned_selection = flow._planned_execution_batch(
            worker_tool=worker_tool,
            stage_cfg=stage_cfg,
            plan_output=plan_output,
            max_workers=max_workers,
            human_instructions=human_instructions,
            execution_target_task_ids=execution_target_task_ids,
        )
    except Exception:
        return static_batch

    if len(planned_selection.task_ids) <= len(static_batch):
        return static_batch

    for task_id, files in planned_selection.hinted_files.items():
        ready_by_id[task_id].files = list(files)

    flow._record_history(
        kind="batch_planning",
        summary="Prepared dynamic execution batch.",
        task_ids=list(planned_selection.task_ids),
        files=planned_selection.planned_files,
        details=[planned_selection.summary],
    )
    return [ready_by_id[task_id] for task_id in planned_selection.task_ids]


def run_structured_do_work(
    flow,
    worker_tool: HeadlessCoderTool,
    stage_cfg,
    plan_output: str,
    human_instructions: str | None = None,
    execution_target_task_ids: list[int] | None = None,
) -> str:
    parallel_cfg = stage_cfg.extra.get("parallel", {}) or {}
    allowed_task_ids = set(execution_target_task_ids or []) or None
    max_workers = 1
    if flow._parallel_do_work_enabled(stage_cfg):
        max_workers = int(parallel_cfg.get("max_workers", 2))
    task_summaries: list[str] = []
    failures: list[str] = []
    batch_counter = 0

    while has_pending_tasks(flow.state.tasks, allowed_task_ids=allowed_task_ids):
        batch = flow._select_structured_execution_batch(
            worker_tool=worker_tool,
            stage_cfg=stage_cfg,
            plan_output=plan_output,
            max_workers=max_workers,
            human_instructions=human_instructions,
            execution_target_task_ids=execution_target_task_ids,
        )
        if not batch:
            pending = [
                task.id
                for task in flow.state.tasks
                if task.status != "done"
                and (allowed_task_ids is None or task.id in allowed_task_ids)
            ]
            failures.append(
                f"No executable task batch found for pending tasks: {pending}"
            )
            break

        for task in batch:
            task.status = "in_progress"

        if len(batch) == 1:
            task = batch[0]
            result, changed_files = flow._run_serial_task(
                worker_tool=worker_tool,
                task=task,
                stage_cfg=stage_cfg,
                plan_output=plan_output,
                human_instructions=human_instructions,
            )
            if result.success:
                flow._mark_task_complete(
                    task,
                    summary=result.summary or result.raw_output,
                    changed_files=changed_files,
                )
                if flow._attempt_structured_cross_task_success_replan(
                    do_work_worker=worker_tool,
                    source_task=task,
                    changed_files=changed_files,
                ):
                    task_summaries.append(
                        f"Task {task.id} complete; replanned remaining work from cross-task changes."
                    )
                    continue
                if flow._attempt_structured_ambiguous_success_replan(
                    do_work_worker=worker_tool,
                    source_task=task,
                    changed_files=changed_files,
                ):
                    task_summaries.append(
                        f"Task {task.id} complete; replanned remaining work from ambiguous success evidence."
                    )
                    continue
                task_summaries.append(
                    f"Task {task.id} complete: {result.summary or result.raw_output}"
                )
            else:
                error = result.error or result.summary or result.raw_output
                if flow._attempt_structured_execution_replan(
                    do_work_worker=worker_tool,
                    failed_task=task,
                    error=error,
                    changed_files=changed_files,
                ):
                    flow._track_changed_files(changed_files)
                    task_summaries.append(
                        f"Task {task.id} failed; replanned remaining work."
                    )
                    continue
                flow._mark_task_failed(
                    task,
                    error=error,
                    changed_files=changed_files,
                )
                failures.append(f"Task {task.id} failed: {error}")
                break
            continue

        target_repo = Path(flow.state.target_repo)
        batch_counter += 1
        batch_id = f"b{batch_counter}"
        workspaces = {
            task.id: create_workspace_copy(
                target_repo,
                prefix=f"flow-parallel-task-{task.id}-",
            )
            for task in batch
        }
        outcomes: list[ParallelTaskOutcome] = []

        try:
            with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                future_map = {
                    pool.submit(
                        flow._run_task_with_change_tracking,
                        worker_tool=worker_tool,
                        task=task,
                        cwd=str(workspaces[task.id]),
                        stage_cfg=stage_cfg,
                        plan_output=plan_output,
                        human_instructions=human_instructions,
                        parallel_batch_id=batch_id,
                    ): task
                    for task in batch
                }
                for future in as_completed(future_map):
                    task = future_map[future]
                    workspace = workspaces[task.id]
                    try:
                        result, changed_files, _created = future.result()
                    except Exception as exc:
                        outcomes.append(
                            ParallelTaskOutcome(
                                task=task,
                                success=False,
                                summary="",
                                changed_files=[],
                                error=str(exc),
                                workspace=str(workspace),
                            )
                        )
                        continue

                    outcomes.append(
                        ParallelTaskOutcome(
                            task=task,
                            success=result.success,
                            summary=result.summary or result.raw_output,
                            changed_files=changed_files,
                            error=result.error or result.summary or result.raw_output,
                            workspace=str(workspace),
                        )
                    )

            conflicts = flow._parallel_conflicts(outcomes)
            ordered_outcomes = sorted(outcomes, key=lambda outcome: outcome.task.id)
            failed_outcomes: list[tuple[ParallelTaskOutcome, str]] = []
            success_replanned_task_id: int | None = None
            success_replanned_reason: str | None = None

            for outcome in ordered_outcomes:
                workspace = Path(outcome.workspace or "")
                if outcome.task.id in conflicts:
                    failed_outcomes.append(
                        (
                            outcome,
                            "Parallel batch changed overlapping files: "
                            + ", ".join(conflicts[outcome.task.id]),
                        )
                    )
                    continue

                if not outcome.success:
                    failed_outcomes.append(
                        (
                            outcome,
                            outcome.error or "Task failed in parallel batch.",
                        )
                    )
                    continue

                denied = match_denied(outcome.changed_files, flow._deny_patterns())
                if denied:
                    # Fail closed: merging an allowed subset would mark
                    # the task complete while silently dropping changes.
                    # Nothing leaves the isolated copy.
                    details = ", ".join(
                        f"{path} (matched {pattern!r})"
                        for path, pattern in sorted(denied.items())
                    )
                    failed_outcomes.append(
                        (outcome, f"Denied paths touched: {details}.")
                    )
                    continue

                try:
                    apply_changed_files(
                        src_root=workspace,
                        dest_root=target_repo,
                        changed_files=outcome.changed_files,
                    )
                except ValueError as exc:
                    failed_outcomes.append(
                        (outcome, f"Mergeback rejected unsafe path: {exc}")
                    )
                    continue
                flow._mark_task_complete(
                    outcome.task,
                    summary=outcome.summary,
                    changed_files=outcome.changed_files,
                )
                task_summaries.append(
                    f"Task {outcome.task.id} complete: {outcome.summary}"
                )

            if failed_outcomes:
                failed_outcome, failed_error = failed_outcomes[0]
                if flow._attempt_structured_execution_replan(
                    do_work_worker=worker_tool,
                    failed_task=failed_outcome.task,
                    error=failed_error,
                    changed_files=failed_outcome.changed_files,
                ):
                    task_summaries.append(
                        f"Task {failed_outcome.task.id} failed; replanned remaining work."
                    )
                    continue

                for outcome, error in failed_outcomes:
                    flow._mark_task_failed(
                        outcome.task,
                        error=error,
                        changed_files=outcome.changed_files,
                    )
                    failures.append(f"Task {outcome.task.id} failed: {error}")
            else:
                for outcome in ordered_outcomes:
                    if flow._attempt_structured_cross_task_success_replan(
                        do_work_worker=worker_tool,
                        source_task=outcome.task,
                        changed_files=outcome.changed_files,
                    ):
                        success_replanned_task_id = outcome.task.id
                        success_replanned_reason = "cross-task changes"
                        break
                    if flow._attempt_structured_ambiguous_success_replan(
                        do_work_worker=worker_tool,
                        source_task=outcome.task,
                        changed_files=outcome.changed_files,
                    ):
                        success_replanned_task_id = outcome.task.id
                        success_replanned_reason = "ambiguous success evidence"
                        break
        finally:
            for workspace in workspaces.values():
                cleanup_workspace_copy(workspace)

        if success_replanned_task_id is not None:
            task_summaries.append(
                f"Task {success_replanned_task_id} complete; replanned remaining work from {success_replanned_reason}."
            )
            continue

        if failures:
            break

    pending = [task.id for task in flow.state.tasks if task.status != "done"]
    if (
        allowed_task_ids is not None
        and pending
        and not has_pending_tasks(flow.state.tasks, allowed_task_ids=allowed_task_ids)
    ):
        remaining_untargeted = [
            task_id for task_id in pending if task_id not in allowed_task_ids
        ]
        if remaining_untargeted:
            task_summaries.append(
                "Focused execution completed the targeted task set; "
                f"remaining untargeted tasks: {remaining_untargeted}"
            )
    if pending:
        task_summaries.append(f"Pending tasks remaining: {pending}")
    if failures:
        flow.state.errors.extend(failures)
        task_summaries.extend(failures)

    return "\n".join(task_summaries).strip() or "No structured tasks were executed."


def execute_do_work(flow, plan_output: str) -> str:
    if flow._is_terminal_status():
        logger.info(
            f"[Flow] Skipping do_work because flow is terminal: {flow.state.status}"
        )
        return flow._terminal_result()
    flow._mark_running()
    flow._log_event("stage_start", stage="do_work")
    worker_tool = flow._get_worker("do_work")
    stage_cfg = flow.config.get_stage("do_work")
    human_gate_message = (
        "About to run the expensive edit stage (do_work). "
        "This will let the headless coder modify files."
    )
    execution_target_task_ids: list[int] | None = None

    while True:
        decision = flow._maybe_ask_human("do_work", human_gate_message)
        if decision.proceed:
            break
        if decision.action == "skip-to-review":
            logger.info("[Flow] Human skipped do_work and routed directly to review.")
            flow.state.last_stage = "do_work"
            flow._refresh_debug_report()
            return (
                "Human skipped do_work before edit stage. "
                "No automated edits were performed in this stage."
            )
        if decision.action == "replan":
            logger.info("[Flow] Human requested replanning before do_work.")
            flow._record_history(
                kind="human_replanning",
                summary="Human requested a fresh plan before do_work.",
                details=[
                    decision.instructions
                    or "Human requested replanning before the edit stage."
                ],
            )
            plan_output = flow._execute_plan_stage(
                human_instructions=decision.instructions,
                current_plan_output=plan_output,
                replanning_reason=(
                    "Human requested replanning before entering do_work."
                ),
            )
            continue
        if decision.action == "target-tasks":
            selected_ids = decision.task_ids or []
            execution_target_task_ids = sorted(
                flow._expand_required_task_ids(set(selected_ids))
            )
            dependency_task_ids = [
                task_id
                for task_id in execution_target_task_ids
                if task_id not in selected_ids
            ]
            reason = (
                decision.instructions
                or "Human selected tasks for focused execution before do_work."
            )
            flow._record_history(
                kind="execution_targeting",
                summary="Human narrowed do_work to targeted tasks.",
                task_ids=execution_target_task_ids,
                files=flow._task_files_for_ids(execution_target_task_ids),
                details=[
                    "Requested tasks: "
                    + ", ".join(str(task_id) for task_id in selected_ids),
                    *(
                        [
                            "Auto-included dependency tasks: "
                            + ", ".join(str(task_id) for task_id in dependency_task_ids)
                        ]
                        if dependency_task_ids
                        else []
                    ),
                    reason,
                ],
            )
            logger.info("[Flow] Human narrowed do_work to targeted tasks.")
            break
        logger.info("[Flow] Human aborted before do_work.")
        flow._mark_human_abort(
            "do_work",
            stage_input=plan_output,
            message=human_gate_message,
        )
        return "aborted-by-human"

    logger.info(f"\n[Flow] do_work using {stage_cfg.worker} (skill: {stage_cfg.skill})")

    if flow.state.tasks:
        flow.state.last_stage = "do_work"
        return flow._run_structured_do_work(
            worker_tool,
            stage_cfg,
            plan_output,
            human_instructions=decision.instructions,
            execution_target_task_ids=execution_target_task_ids,
        )

    human_guidance = (
        f"\nHuman approval instructions:\n- {decision.instructions}\n"
        if decision.instructions
        else ""
    )

    prompt = f"""Follow the assigned operating procedure for this implementation stage.

Plan / spec context:
{plan_output[:3000]}

Original user request:
{flow.state.request}

Target repo: {flow.state.target_repo}

Current revision count: {flow.state.revisions}
{human_guidance}

Execute the work. After you are done, summarize what changed and whether tests now pass.
"""

    result, changed_files = flow._run_unstructured_edit(worker_tool, stage_cfg, prompt)

    if changed_files:
        flow.state.changed_files.extend(changed_files)

    flow.state.last_stage = "do_work"
    return result.summary or result.raw_output or result.error or ""


# ------------------------------------------------------------------
# Review router - uses configured worker in INSPECT (read-only) mode
# ------------------------------------------------------------------
