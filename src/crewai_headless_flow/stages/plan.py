"""Plan stage body and helpers (Phase 1 extraction)."""

from __future__ import annotations

import logging
from typing import Any

from ..plan_contract import (
    PlanOutput,
    normalize_plan_output,
    plan_tasks_to_state_items,
    require_concrete_plan,
    render_plan_markdown,
)
from ..plan_crew import run_plan_crew

logger = logging.getLogger(__name__)


def plan_crew_enabled(flow: Any, stage_cfg: Any) -> bool:
    crew_cfg = stage_cfg.extra.get("crew", {}) or {}
    return bool(crew_cfg.get("enabled", False))


def current_task_graph_summary(flow: Any) -> str:
    if not flow.state.tasks:
        return "- No current structured tasks"
    lines = []
    for task in flow.state.tasks:
        files = ", ".join(task.files) if task.files else "unknown"
        lines.append(
            f"- Task {task.id}: {task.title or task.description} "
            f"| status={task.status} | files={files}"
        )
    return "\n".join(lines)


def build_plan_prompt(
    flow: Any,
    human_instructions: str | None = None,
    *,
    current_plan_output: str | None = None,
    replanning_reason: str | None = None,
) -> str:
    human_guidance = (
        f"\nHuman approval instructions:\n- {human_instructions}\n"
        if human_instructions
        else ""
    )
    current_plan_context = ""
    if current_plan_output or flow.state.tasks:
        current_plan_context = (
            f"\nCurrent saved plan/task graph:\n{flow._current_task_graph_summary()}\n"
        )
        if current_plan_output:
            current_plan_context += (
                f"\nCurrent rendered plan markdown:\n{current_plan_output[:3000]}\n"
            )
    replanning_context = (
        f"\nReplanning context:\n- {replanning_reason}\n" if replanning_reason else ""
    )
    return f"""Create a structured implementation plan for this repository request.

Original user request:
{flow.state.request}

Target repository: {flow.state.target_repo}

Inspect relevant files as needed so the plan is grounded in the actual codebase.
{current_plan_context}{replanning_context}{human_guidance}

Produce a single structured plan with:
- `spec`: concise but complete objective, success criteria, and boundaries
- `tasks`: small vertical slices with explicit acceptance criteria, verification, dependencies, likely files, and estimated scope
""".strip()


def execute_plan_stage(
    flow: Any,
    *,
    human_instructions: str | None = None,
    current_plan_output: str | None = None,
    replanning_reason: str | None = None,
) -> str:
    stage_cfg = flow.config.get_stage("plan")
    worker_tool = flow._get_worker("plan")
    prompt = build_plan_prompt(
        flow,
        human_instructions,
        current_plan_output=current_plan_output,
        replanning_reason=replanning_reason,
    )

    if flow._plan_crew_enabled(stage_cfg):
        try:
            plan = run_plan_crew(
                planning_context=prompt,
                worker_tool=worker_tool,
                cwd=flow.state.target_repo,
                timeout=stage_cfg.timeout,
                model=stage_cfg.model,
                crew_config=stage_cfg.extra.get("crew", {}) or {},
                config_dir=flow.state.config_dir,
            )
        except Exception as exc:
            raise RuntimeError(f"Planning Crew failed: {exc}") from exc
    else:
        result = worker_tool.run(
            task=prompt,
            cwd=flow.state.target_repo,
            mode="inspect",
            schema=PlanOutput.model_json_schema(),
            model=stage_cfg.model,
            timeout=stage_cfg.timeout,
        )
        if not result.success:
            error = result.error or result.summary or result.raw_output
            raise RuntimeError(f"Planning stage failed: {error}")

        plan = normalize_plan_output([result.summary, result.raw_output])

    try:
        plan = require_concrete_plan(plan)
    except ValueError as exc:
        raise RuntimeError(
            f"Planning stage returned an invalid structured plan: {exc}"
        ) from exc

    output = render_plan_markdown(plan)

    flow.state.spec = plan.spec
    flow.state.tasks = plan_tasks_to_state_items(plan.tasks)
    flow.state.last_stage = "plan"
    flow._refresh_debug_report()

    logger.info(
        f"\n[Flow] Planning complete. "
        f"Spec length: {len(plan.spec)} | Tasks: {len(plan.tasks)}"
    )
    return output


def execute_plan(flow: Any) -> str:
    flow._mark_running()
    flow._log_event("stage_start", stage="plan")
    flow.config.print_mapping()  # Visibility into current wiring
    human_gate_message = (
        "About to run planning stage (plan). "
        "This is read-only but may inspect a broad slice of the repository."
    )
    decision = flow._maybe_ask_human(
        "plan",
        human_gate_message,
    )
    if not decision.proceed:
        logger.info("[Flow] Human aborted before plan.")
        flow._mark_human_abort("plan", message=human_gate_message)
        return "aborted-by-human"
    return flow._execute_plan_stage(human_instructions=decision.instructions)
