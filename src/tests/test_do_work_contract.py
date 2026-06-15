from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from crewai_headless_flow.do_work_contract import (
    DoWorkExecutionPlan,
    DoWorkSubtask,
    normalize_do_work_execution_plan,
    require_concrete_do_work_execution_plan,
)


pytestmark = pytest.mark.offline


def test_normalize_do_work_execution_plan_accepts_pydantic_result():
    output = SimpleNamespace(
        pydantic=DoWorkExecutionPlan(
            summary="Split task into slices.",
            subtasks=[
                DoWorkSubtask(
                    id=1,
                    title="Add contract",
                    description="Create typed contract.",
                    files=["src/contract.py"],
                    verification=["pytest -q"],
                )
            ],
        )
    )

    plan = normalize_do_work_execution_plan(output)

    assert plan is not None
    assert plan.summary == "Split task into slices."
    assert plan.subtasks[0].title == "Add contract"


def test_normalize_do_work_execution_plan_unwraps_nested_json():
    raw = json.dumps(
        {
            "result": {
                "summary": "Use two slices.",
                "subtasks": [
                    {
                        "id": 1,
                        "title": "First",
                        "description": "Do first thing.",
                        "files": ["src/a.py"],
                        "verification": ["pytest -q"],
                    }
                ],
            }
        }
    )

    plan = normalize_do_work_execution_plan(raw)

    assert plan is not None
    assert plan.subtasks[0].description == "Do first thing."


def test_normalize_do_work_execution_plan_skips_invalid_candidate_and_uses_later_valid_one():
    plan = normalize_do_work_execution_plan(
        [
            '{"summary": "Broken", "subtasks": [{"id": "oops", "title": "Bad", "description": "Bad"}]}',
            json.dumps(
                {
                    "summary": "Use two slices.",
                    "subtasks": [
                        {
                            "id": 1,
                            "title": "First",
                            "description": "Do first thing.",
                        }
                    ],
                }
            ),
        ]
    )

    assert plan is not None
    assert plan.summary == "Use two slices."
    assert plan.subtasks[0].id == 1


def test_normalize_do_work_execution_plan_ignores_invalid_direct_payload_and_uses_nested_valid_one():
    plan = normalize_do_work_execution_plan(
        {
            "summary": "Broken",
            "subtasks": [{"id": "oops", "title": "Bad", "description": "Bad"}],
            "result": {
                "summary": "Use two slices.",
                "subtasks": [
                    {
                        "id": 1,
                        "title": "First",
                        "description": "Do first thing.",
                    }
                ],
            },
        }
    )

    assert plan is not None
    assert plan.summary == "Use two slices."
    assert plan.subtasks[0].id == 1


def test_require_concrete_do_work_execution_plan_rejects_bad_shape():
    with pytest.raises(
        ValueError,
        match="task decomposition plan exceeds max_subtasks=1",
    ):
        require_concrete_do_work_execution_plan(
            DoWorkExecutionPlan(
                summary="Too many slices.",
                subtasks=[
                    DoWorkSubtask(id=1, title="One", description="One"),
                    DoWorkSubtask(id=2, title="Two", description="Two"),
                ],
            ),
            max_subtasks=1,
        )

    with pytest.raises(
        ValueError,
        match="task decomposition subtask ids must be unique",
    ):
        require_concrete_do_work_execution_plan(
            DoWorkExecutionPlan(
                summary="Duplicate ids.",
                subtasks=[
                    DoWorkSubtask(id=1, title="One", description="One"),
                    DoWorkSubtask(id=1, title="Again", description="Again"),
                ],
            ),
            max_subtasks=4,
        )
