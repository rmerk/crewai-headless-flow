"""Structured contract for optional task-local decomposition in do_work."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field


class DoWorkSubtask(BaseModel):
    """One bounded execution slice for a planned task."""

    id: int
    title: str
    description: str
    files: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)


class DoWorkExecutionPlan(BaseModel):
    """Structured decomposition for a single do_work task."""

    summary: str
    subtasks: list[DoWorkSubtask] = Field(default_factory=list)


DO_WORK_EXECUTION_PLAN_SCHEMA: dict[str, Any] = DoWorkExecutionPlan.model_json_schema()
DO_WORK_EXECUTION_PLAN_SCHEMA["additionalProperties"] = False


def normalize_do_work_execution_plan(output: Any) -> DoWorkExecutionPlan | None:
    """Normalize worker or Crew output into a task-local execution plan."""

    for candidate in _collect_candidates(output):
        plan = _normalize_candidate(candidate)
        if plan is not None:
            return plan
    return None


def require_concrete_do_work_execution_plan(
    plan: DoWorkExecutionPlan,
    *,
    max_subtasks: int,
) -> DoWorkExecutionPlan:
    """Validate optional decomposition output before using it."""

    if max_subtasks < 1:
        raise ValueError(
            "Implementation Crew decomposition max_subtasks must be at least 1"
        )
    if not plan.summary.strip():
        raise ValueError("task decomposition plan is missing a summary")
    if not plan.subtasks:
        raise ValueError("task decomposition plan must include at least one subtask")
    if len(plan.subtasks) > max_subtasks:
        raise ValueError(f"task decomposition plan exceeds max_subtasks={max_subtasks}")

    seen_ids: set[int] = set()
    for subtask in plan.subtasks:
        if subtask.id <= 0:
            raise ValueError(
                f"task decomposition subtask ids must be positive integers, got {subtask.id}"
            )
        if subtask.id in seen_ids:
            raise ValueError(
                f"task decomposition subtask ids must be unique, got duplicate {subtask.id}"
            )
        if not subtask.title.strip():
            raise ValueError(
                f"task decomposition subtask {subtask.id} is missing a title"
            )
        if not subtask.description.strip():
            raise ValueError(
                f"task decomposition subtask {subtask.id} is missing a description"
            )
        seen_ids.add(subtask.id)

    return plan


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

        for attr in ("pydantic", "json_dict", "raw", "result", "content", "message"):
            value = getattr(candidate, attr, None)
            if value is not None:
                queue.append(value)

    return candidates


def _normalize_candidate(candidate: Any) -> DoWorkExecutionPlan | None:
    if isinstance(candidate, DoWorkExecutionPlan):
        return candidate

    if isinstance(candidate, BaseModel):
        candidate = candidate.model_dump()

    if isinstance(candidate, dict):
        return _plan_from_dict(candidate)

    if isinstance(candidate, str):
        loaded = _load_json_object(candidate)
        if loaded is None:
            return None
        return _plan_from_dict(loaded)

    return None


def _plan_from_dict(data: dict[str, Any]) -> DoWorkExecutionPlan | None:
    if "summary" in data and "subtasks" in data:
        direct = _validate_plan_payload(data)
        if direct is not None:
            return direct

    for key in ("result", "text", "content", "message", "summary", "raw"):
        value = data.get(key)
        if isinstance(value, dict):
            nested = _plan_from_dict(value)
            if nested is not None:
                return nested
        elif isinstance(value, str):
            loaded = _load_json_object(value)
            if loaded is not None:
                nested = _plan_from_dict(loaded)
                if nested is not None:
                    return nested

    return None


def _validate_plan_payload(data: dict[str, Any]) -> DoWorkExecutionPlan | None:
    try:
        return DoWorkExecutionPlan.model_validate(data)
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
