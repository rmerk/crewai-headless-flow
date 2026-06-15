"""Shared contract for structured planning output."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from .state import TaskItem


class PlanTask(BaseModel):
    """Structured planning task emitted by the planning stage."""

    id: int
    title: str | None = None
    description: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    dependencies: list[int] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    estimated_scope: str | None = None


class PlanOutput(BaseModel):
    """Structured output from the planning stage."""

    spec: str
    tasks: list[PlanTask] = Field(default_factory=list)


def normalize_plan_output(output: Any) -> PlanOutput:
    """Normalize CrewAI task output into a structured plan."""

    for candidate in _collect_candidates(output):
        plan = _normalize_candidate(candidate)
        if plan is not None:
            return plan

    return PlanOutput(spec="", tasks=[])


def require_concrete_plan(plan: PlanOutput) -> PlanOutput:
    """Fail closed if a supposedly structured plan is empty or malformed."""

    if not plan.spec.strip():
        raise ValueError("plan output is missing a non-empty spec")
    if not plan.tasks:
        raise ValueError("plan output must include at least one task")

    seen_ids: set[int] = set()
    for task in plan.tasks:
        if task.id <= 0:
            raise ValueError(f"plan task ids must be positive integers, got {task.id}")
        if task.id in seen_ids:
            raise ValueError(f"plan task ids must be unique, got duplicate {task.id}")
        if not task.description.strip():
            raise ValueError(f"plan task {task.id} is missing a description")
        seen_ids.add(task.id)

    return plan


def render_plan_markdown(plan: PlanOutput) -> str:
    """Render structured plan back into stable markdown for downstream prompts."""

    lines = ["<spec>", plan.spec.strip(), "</spec>", "", "<tasks>"]
    for task in plan.tasks:
        title = task.title or task.description
        lines.append(f"{task.id}. {title}")
        lines.append(f"   Description: {task.description}")
        if task.acceptance_criteria:
            lines.append("   Acceptance criteria:")
            lines.extend(f"   - {criterion}" for criterion in task.acceptance_criteria)
        if task.verification:
            lines.append("   Verification:")
            lines.extend(f"   - {check}" for check in task.verification)
        if task.dependencies:
            deps = ", ".join(str(dep) for dep in task.dependencies)
            lines.append(f"   Dependencies: {deps}")
        if task.files:
            lines.append("   Files likely touched:")
            lines.extend(f"   - {path}" for path in task.files)
        if task.estimated_scope:
            lines.append(f"   Estimated scope: {task.estimated_scope}")
        lines.append("")

    lines.append("</tasks>")
    return "\n".join(lines).strip()


def plan_tasks_to_state_items(tasks: list[PlanTask]) -> list[TaskItem]:
    return [
        TaskItem(
            id=task.id,
            title=task.title,
            description=task.description,
            acceptance_criteria=task.acceptance_criteria,
            verification=task.verification,
            dependencies=task.dependencies,
            files=task.files,
            estimated_scope=task.estimated_scope,
        )
        for task in tasks
    ]


def state_items_to_plan_output(spec: str | None, tasks: list[TaskItem]) -> PlanOutput:
    return PlanOutput(
        spec=(spec or "").strip(),
        tasks=[
            PlanTask(
                id=task.id,
                title=task.title,
                description=task.description,
                acceptance_criteria=task.acceptance_criteria,
                verification=task.verification,
                dependencies=task.dependencies,
                files=task.files,
                estimated_scope=task.estimated_scope,
            )
            for task in tasks
        ],
    )


def _collect_candidates(output: Any) -> list[Any]:
    queue: list[Any] = [output]
    candidates: list[Any] = []

    while queue:
        candidate = queue.pop(0)
        if candidate is None:
            continue
        if isinstance(candidate, (list, tuple)):
            queue[:0] = list(candidate)
            continue

        candidates.append(candidate)

        if isinstance(candidate, (str, dict, BaseModel)):
            continue

        for attr in ("pydantic", "json_dict", "raw", "result"):
            value = getattr(candidate, attr, None)
            if value is not None:
                queue.append(value)

    return candidates


def _normalize_candidate(candidate: Any) -> PlanOutput | None:
    if isinstance(candidate, PlanOutput):
        return candidate

    if isinstance(candidate, BaseModel):
        candidate = candidate.model_dump()

    if isinstance(candidate, dict):
        return _plan_from_dict(candidate)

    if isinstance(candidate, str):
        loaded = _load_json_object(candidate)
        if loaded is not None:
            return _plan_from_dict(loaded)
        return _legacy_plan_from_text(candidate)

    return None


def _plan_from_dict(data: dict[str, Any]) -> PlanOutput | None:
    if "spec" in data and "tasks" in data:
        direct = _validate_plan_payload(data)
        if direct is not None:
            return direct

    for key in ("result", "content", "message", "raw"):
        value = data.get(key)
        if isinstance(value, dict):
            nested = _plan_from_dict(value)
            if nested is not None:
                return nested
        elif isinstance(value, str):
            nested_loaded = _load_json_object(value)
            if nested_loaded is not None:
                nested = _plan_from_dict(nested_loaded)
                if nested is not None:
                    return nested
            legacy = _legacy_plan_from_text(value)
            if legacy is not None:
                return legacy

    return None


def _validate_plan_payload(data: dict[str, Any]) -> PlanOutput | None:
    try:
        return PlanOutput.model_validate(data)
    except Exception:
        return None


def _load_json_object(raw: str) -> dict[str, Any] | None:
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start == -1 or end <= start:
        return None

    try:
        loaded = json.loads(raw[start:end])
    except json.JSONDecodeError:
        return None

    return loaded if isinstance(loaded, dict) else None


def _legacy_plan_from_text(raw: str) -> PlanOutput | None:
    raw = raw.strip()
    if not raw:
        return None

    spec_match = re.search(r"<spec>\s*(.*?)\s*</spec>", raw, re.DOTALL | re.IGNORECASE)
    tasks_match = re.search(
        r"<tasks>\s*(.*?)\s*</tasks>", raw, re.DOTALL | re.IGNORECASE
    )

    spec = spec_match.group(1).strip() if spec_match else raw
    tasks_block = tasks_match.group(1).strip() if tasks_match else ""

    tasks: list[PlanTask] = []
    for line in tasks_block.splitlines():
        match = re.match(r"^\s*(\d+)[\).\:-]\s+(.*)$", line.strip())
        if not match:
            continue
        task_id = int(match.group(1))
        description = match.group(2).strip()
        tasks.append(
            PlanTask(
                id=task_id,
                title=description,
                description=description,
            )
        )

    return PlanOutput(spec=spec, tasks=tasks)
