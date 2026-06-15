"""Structured contract for dynamic cross-task batching in do_work."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field


class DoWorkBatchTask(BaseModel):
    """One task selected for the next execution batch."""

    task_id: int
    files: list[str] = Field(default_factory=list)


class DoWorkBatchPlan(BaseModel):
    """Structured selection for the next do_work execution batch."""

    summary: str
    tasks: list[DoWorkBatchTask] = Field(default_factory=list)


DO_WORK_BATCH_PLAN_SCHEMA: dict[str, Any] = DoWorkBatchPlan.model_json_schema()
DO_WORK_BATCH_PLAN_SCHEMA["additionalProperties"] = False


def normalize_do_work_batch_plan(output: Any) -> DoWorkBatchPlan | None:
    """Normalize worker output into a do_work batch plan."""

    for candidate in _collect_candidates(output):
        plan = _normalize_candidate(candidate)
        if plan is not None:
            return plan
    return None


def require_concrete_do_work_batch_plan(
    plan: DoWorkBatchPlan,
    *,
    allowed_task_ids: set[int],
    max_workers: int,
) -> DoWorkBatchPlan:
    """Validate dynamic batch plan before using it."""

    if max_workers < 1:
        raise ValueError("do_work batch planner max_workers must be at least 1")
    if not plan.summary.strip():
        raise ValueError("do_work batch plan is missing a summary")
    if not plan.tasks:
        raise ValueError("do_work batch plan must include at least one task")
    if len(plan.tasks) > max_workers:
        raise ValueError(f"do_work batch plan exceeds max_workers={max_workers}")

    seen_ids: set[int] = set()
    for task in plan.tasks:
        if task.task_id <= 0:
            raise ValueError(
                f"do_work batch plan task ids must be positive integers, got {task.task_id}"
            )
        if task.task_id in seen_ids:
            raise ValueError(
                f"do_work batch plan task ids must be unique, got duplicate {task.task_id}"
            )
        if task.task_id not in allowed_task_ids:
            raise ValueError(
                f"do_work batch plan selected unknown or non-ready task id {task.task_id}"
            )
        seen_ids.add(task.task_id)

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


def _normalize_candidate(candidate: Any) -> DoWorkBatchPlan | None:
    if isinstance(candidate, DoWorkBatchPlan):
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


def _plan_from_dict(data: dict[str, Any]) -> DoWorkBatchPlan | None:
    if "summary" in data and "tasks" in data:
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


def _validate_plan_payload(data: dict[str, Any]) -> DoWorkBatchPlan | None:
    try:
        return DoWorkBatchPlan.model_validate(data)
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
