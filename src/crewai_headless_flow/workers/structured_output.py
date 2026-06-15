"""Shared structured-output extraction and lightweight JSON schema validation."""

from __future__ import annotations

import json
from typing import Any


WRAPPER_KEYS = ("result", "text", "content", "message", "summary", "raw")


def build_repair_prompt(task: str, schema: dict[str, Any], previous_output: str) -> str:
    return (
        "The previous response did not match the required JSON schema.\n\n"
        f"Original task:\n{task}\n\n"
        f"Required schema:\n{json.dumps(schema, indent=2, sort_keys=True)}\n\n"
        "Previous output:\n"
        f"{previous_output[:4000]}\n\n"
        "Return ONLY a single valid JSON object matching the schema. "
        "Do not include markdown fences or any extra text."
    )


def extract_validated_json(raw: str, schema: dict[str, Any] | None) -> str | None:
    if not schema:
        return None

    for candidate in _candidate_objects_from_raw(raw):
        if _validate_schema(candidate, schema, schema):
            return json.dumps(candidate, indent=2, sort_keys=True)

    return None


def _candidate_objects_from_raw(raw: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if not isinstance(value, dict):
            return
        marker = json.dumps(value, sort_keys=True)
        if marker in seen:
            return
        seen.add(marker)
        candidates.append(value)

    direct = _load_json_value(raw.strip())
    if direct is not None:
        for candidate in _iter_payload_candidates(direct):
            add(candidate)

    for line in raw.splitlines():
        loaded = _load_json_value(line.strip())
        if loaded is not None:
            for candidate in _iter_payload_candidates(loaded):
                add(candidate)

    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start != -1 and end > start:
        loaded = _load_json_value(raw[start:end])
        if loaded is not None:
            for candidate in _iter_payload_candidates(loaded):
                add(candidate)

    return candidates


def _iter_payload_candidates(value: Any, depth: int = 0):
    if depth > 4 or value is None:
        return

    if isinstance(value, dict):
        yield value
        for key in WRAPPER_KEYS:
            nested = value.get(key)
            if nested is not None:
                yield from _iter_payload_candidates(nested, depth + 1)
        for nested in value.values():
            if isinstance(nested, (dict, list, str)):
                yield from _iter_payload_candidates(nested, depth + 1)
        return

    if isinstance(value, list):
        for item in value:
            yield from _iter_payload_candidates(item, depth + 1)
        return

    if isinstance(value, str):
        loaded = _load_json_value(value)
        if loaded is not None:
            yield from _iter_payload_candidates(loaded, depth + 1)


def _load_json_value(raw: str) -> Any | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _validate_schema(value: Any, schema: dict[str, Any], root: dict[str, Any]) -> bool:
    if "$ref" in schema:
        resolved = _resolve_ref(schema["$ref"], root)
        return resolved is not None and _validate_schema(value, resolved, root)

    if "anyOf" in schema:
        return any(_validate_schema(value, option, root) for option in schema["anyOf"])

    if "allOf" in schema:
        return all(_validate_schema(value, option, root) for option in schema["allOf"])

    if "enum" in schema and value not in schema["enum"]:
        return False

    schema_type = schema.get("type")
    if schema_type == "object" or "properties" in schema or "required" in schema:
        return _validate_object(value, schema, root)
    if schema_type == "array" or "items" in schema:
        return _validate_array(value, schema, root)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(
            value, float
        )
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "null":
        return value is None

    return True


def _validate_object(value: Any, schema: dict[str, Any], root: dict[str, Any]) -> bool:
    if not isinstance(value, dict):
        return False

    required = schema.get("required", [])
    if any(key not in value for key in required):
        return False

    properties = schema.get("properties", {}) or {}
    additional_properties = schema.get("additionalProperties", True)
    if additional_properties is False:
        allowed_keys = set(properties)
        if any(key not in allowed_keys for key in value):
            return False

    for key, subschema in properties.items():
        if key in value and not _validate_schema(value[key], subschema, root):
            return False

    return True


def _validate_array(value: Any, schema: dict[str, Any], root: dict[str, Any]) -> bool:
    if not isinstance(value, list):
        return False

    items = schema.get("items")
    if items is None:
        return True

    return all(_validate_schema(item, items, root) for item in value)


def _resolve_ref(ref: str, root: dict[str, Any]) -> dict[str, Any] | None:
    if not ref.startswith("#/"):
        return None

    cursor: Any = root
    for token in ref[2:].split("/"):
        if not isinstance(cursor, dict) or token not in cursor:
            return None
        cursor = cursor[token]

    return cursor if isinstance(cursor, dict) else None
