"""Shared contract for review-stage decisions across direct and Crew paths."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


DEFAULT_PARSE_FAILURE_ISSUE = "Review output could not be parsed as structured JSON"
INVALID_TASK_HINTS_ISSUE = (
    "Review returned invalid task_hints; ignored them and failed closed"
)


class ReviewTaskHint(BaseModel):
    """Optional mapping from review feedback to planned tasks/files."""

    task_ids: list[int] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    summary: str


class ReviewDecision(BaseModel):
    """Normalized review decision consumed by the Flow router."""

    status: Literal["pass", "revise"]
    issues: list[str] = Field(default_factory=list)
    summary: str
    task_hints: list[ReviewTaskHint] = Field(default_factory=list)

    @model_validator(mode="after")
    def fail_closed_when_issues_remain(self) -> "ReviewDecision":
        if self.status == "pass" and (self.issues or self.task_hints):
            self.status = "revise"
        return self


REVIEW_DECISION_SCHEMA: dict[str, Any] = ReviewDecision.model_json_schema()
REVIEW_DECISION_SCHEMA["additionalProperties"] = False


def fail_closed_review_decision(
    issue: str = DEFAULT_PARSE_FAILURE_ISSUE,
) -> ReviewDecision:
    return ReviewDecision(status="revise", issues=[issue], summary=issue)


def normalize_review_output(
    output: Any,
    *,
    parse_failure_issue: str = DEFAULT_PARSE_FAILURE_ISSUE,
) -> ReviewDecision:
    """Normalize arbitrary worker/Crew output into one review contract."""

    for candidate in _collect_candidates(output):
        decision = _normalize_review_candidate(candidate)
        if decision is not None:
            return decision

    return fail_closed_review_decision(parse_failure_issue)


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


def _normalize_review_candidate(candidate: Any) -> ReviewDecision | None:
    if isinstance(candidate, ReviewDecision):
        return candidate

    if isinstance(candidate, BaseModel):
        candidate = candidate.model_dump()

    if isinstance(candidate, dict):
        payload = _extract_review_payload(candidate)
        if payload is None:
            return None
        try:
            return _decision_from_payload(payload)
        except Exception:
            return None

    if isinstance(candidate, str):
        loaded = _load_json_object(candidate)
        if loaded is None:
            return None
        payload = _extract_review_payload(loaded)
        if payload is None:
            return None
        try:
            return _decision_from_payload(payload)
        except Exception:
            return None

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


def _extract_review_payload(data: dict[str, Any]) -> dict[str, Any] | None:
    if "status" in data:
        return data

    for key in ("result", "text", "content", "message", "summary", "raw"):
        value = data.get(key)
        if isinstance(value, dict):
            nested = _extract_review_payload(value)
            if nested is not None:
                return nested
        elif isinstance(value, str):
            loaded = _load_json_object(value)
            if loaded is not None:
                nested = _extract_review_payload(loaded)
                if nested is not None:
                    return nested

    return None


def _decision_from_payload(payload: dict[str, Any]) -> ReviewDecision:
    raw_status = payload.get("status")
    status: Literal["pass", "revise"] = "pass" if raw_status == "pass" else "revise"

    raw_issues = payload.get("issues", [])
    if isinstance(raw_issues, list):
        issues = [str(issue) for issue in raw_issues]
    elif raw_issues:
        issues = [str(raw_issues)]
    else:
        issues = []

    task_hints, invalid_task_hints = _normalize_task_hints(
        payload.get("task_hints", [])
    )

    summary = _coerce_summary(payload.get("summary"), issues)

    if raw_status not in {"pass", "revise"}:
        issues = [f"Review returned invalid status: {raw_status}"]
    if invalid_task_hints:
        issues = [*issues, INVALID_TASK_HINTS_ISSUE]
        status = "revise"

    return ReviewDecision(
        status=status,
        issues=issues,
        summary=summary,
        task_hints=task_hints,
    )


def _coerce_summary(raw_summary: Any, issues: list[str]) -> str:
    if isinstance(raw_summary, str) and raw_summary.strip():
        return raw_summary.strip()
    if raw_summary is not None:
        return json.dumps(raw_summary, indent=2)
    if issues:
        return issues[0]
    return "Review completed."


def _normalize_task_hints(raw_task_hints: Any) -> tuple[list[ReviewTaskHint], bool]:
    if isinstance(raw_task_hints, dict):
        candidates = [raw_task_hints]
    elif isinstance(raw_task_hints, list):
        candidates = raw_task_hints
    elif raw_task_hints in (None, ""):
        return [], False
    else:
        return [], True

    normalized: list[ReviewTaskHint] = []
    invalid = False
    for raw_hint in candidates:
        try:
            normalized.append(ReviewTaskHint.model_validate(raw_hint))
        except Exception:
            invalid = True

    return normalized, invalid
