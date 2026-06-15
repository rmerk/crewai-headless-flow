from __future__ import annotations

import json

import pytest

from crewai_headless_flow.review_contract import REVIEW_DECISION_SCHEMA
from crewai_headless_flow.workers.structured_output import extract_validated_json


pytestmark = pytest.mark.offline


def test_extract_validated_json_accepts_review_payload_with_task_hints():
    raw = json.dumps(
        {
            "status": "revise",
            "issues": ["Missing test"],
            "summary": "Needs test coverage.",
            "task_hints": [
                {
                    "task_ids": [1],
                    "files": ["src/example.py"],
                    "summary": "Task 1 needs revision",
                }
            ],
        }
    )

    parsed = extract_validated_json(raw, REVIEW_DECISION_SCHEMA)

    assert parsed is not None
    assert '"task_hints"' in parsed
    assert '"task_ids"' in parsed


def test_extract_validated_json_unwraps_nested_cli_wrapper():
    raw = json.dumps(
        {
            "result": {
                "status": "pass",
                "issues": [],
                "summary": "Looks good.",
                "task_hints": [],
            }
        }
    )

    parsed = extract_validated_json(raw, REVIEW_DECISION_SCHEMA)

    assert parsed is not None
    assert '"status": "pass"' in parsed


def test_extract_validated_json_rejects_missing_required_fields():
    raw = json.dumps({"status": "pass", "issues": []})

    assert extract_validated_json(raw, REVIEW_DECISION_SCHEMA) is None


def test_extract_validated_json_supports_anyof_values():
    schema = {
        "type": "object",
        "properties": {
            "summary": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "null"},
                ]
            }
        },
        "required": ["summary"],
        "additionalProperties": False,
    }

    parsed = extract_validated_json('{"summary": null}', schema)

    assert parsed is not None
