from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from crewai_headless_flow.review_contract import (
    INVALID_TASK_HINTS_ISSUE,
    ReviewDecision,
    ReviewTaskHint,
    fail_closed_review_decision,
    normalize_review_output,
)


pytestmark = pytest.mark.offline


def test_normalize_review_output_accepts_plain_json():
    decision = normalize_review_output(
        '{"status": "pass", "issues": [], "summary": "Looks good"}'
    )

    assert decision.status == "pass"
    assert decision.issues == []
    assert decision.summary == "Looks good"


def test_normalize_review_output_accepts_wrapped_json_payload():
    inner = '{"status": "pass", "issues": [], "summary": "Looks good"}'

    decision = normalize_review_output(json.dumps({"type": "result", "result": inner}))

    assert decision.status == "pass"
    assert decision.issues == []


def test_normalize_review_output_invalid_status_fails_closed_to_revise():
    decision = normalize_review_output(
        '{"status": "approve", "issues": [], "summary": "Bad status"}'
    )

    assert decision.status == "revise"
    assert decision.issues == ["Review returned invalid status: approve"]


def test_normalize_review_output_coerces_scalar_issues_to_list():
    decision = normalize_review_output(
        '{"status": "revise", "issues": "Missing tests", "summary": "Needs work"}'
    )

    assert decision.status == "revise"
    assert decision.issues == ["Missing tests"]


def test_normalize_review_output_converts_pass_with_issues_to_revise():
    decision = normalize_review_output(
        ReviewDecision(
            status="pass",
            issues=["Concrete issue remains"],
            summary="Contradictory output.",
        )
    )

    assert decision.status == "revise"
    assert decision.issues == ["Concrete issue remains"]


def test_normalize_review_output_accepts_task_hints():
    decision = normalize_review_output(
        """
        {
          "status": "revise",
          "issues": ["Fix task 2"],
          "summary": "Needs revision.",
          "task_hints": [
            {"task_ids": [2], "files": ["src/flow.py"], "summary": "Retry task 2"}
          ]
        }
        """
    )

    assert decision.status == "revise"
    assert decision.task_hints == [
        ReviewTaskHint(
            task_ids=[2],
            files=["src/flow.py"],
            summary="Retry task 2",
        )
    ]


def test_normalize_review_output_invalid_task_hints_fail_closed_without_raising():
    decision = normalize_review_output(
        """
        {
          "status": "pass",
          "issues": [],
          "summary": "Looks good",
          "task_hints": [
            {"task_ids": "oops", "summary": "Bad mapping"}
          ]
        }
        """
    )

    assert decision.status == "revise"
    assert decision.task_hints == []
    assert INVALID_TASK_HINTS_ISSUE in decision.issues


def test_normalize_review_output_keeps_valid_task_hints_and_flags_invalid_ones():
    decision = normalize_review_output(
        """
        {
          "status": "revise",
          "issues": ["Need another test"],
          "summary": "Needs work",
          "task_hints": [
            {"task_ids": [2], "files": ["src/flow.py"], "summary": "Retry task 2"},
            {"task_ids": "oops", "summary": "Bad mapping"}
          ]
        }
        """
    )

    assert decision.status == "revise"
    assert decision.task_hints == [
        ReviewTaskHint(
            task_ids=[2],
            files=["src/flow.py"],
            summary="Retry task 2",
        )
    ]
    assert "Need another test" in decision.issues
    assert INVALID_TASK_HINTS_ISSUE in decision.issues


def test_normalize_review_output_reads_namespace_candidates():
    output = SimpleNamespace(
        pydantic=None,
        json_dict=None,
        raw='{"status": "pass", "issues": [], "summary": "Looks good"}',
    )

    decision = normalize_review_output(output)

    assert decision.status == "pass"
    assert decision.summary == "Looks good"


def test_normalize_review_output_fails_closed_on_malformed_output():
    decision = normalize_review_output("not json")

    assert decision == fail_closed_review_decision()
