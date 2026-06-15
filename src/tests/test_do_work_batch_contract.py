from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from crewai_headless_flow.do_work_batch_contract import (
    DoWorkBatchPlan,
    DoWorkBatchTask,
    normalize_do_work_batch_plan,
    require_concrete_do_work_batch_plan,
)


pytestmark = pytest.mark.offline


def test_normalize_do_work_batch_plan_accepts_pydantic_result():
    output = SimpleNamespace(
        pydantic=DoWorkBatchPlan(
            summary="Run tasks 1 and 2 together.",
            tasks=[
                DoWorkBatchTask(task_id=1, files=["src/a.py"]),
                DoWorkBatchTask(task_id=2, files=["src/b.py"]),
            ],
        )
    )

    plan = normalize_do_work_batch_plan(output)

    assert plan is not None
    assert plan.summary == "Run tasks 1 and 2 together."
    assert [task.task_id for task in plan.tasks] == [1, 2]


def test_normalize_do_work_batch_plan_unwraps_nested_json():
    raw = json.dumps(
        {
            "result": {
                "summary": "Run task 2 next.",
                "tasks": [{"task_id": 2, "files": ["src/b.py"]}],
            }
        }
    )

    plan = normalize_do_work_batch_plan(raw)

    assert plan is not None
    assert plan.tasks[0].task_id == 2


def test_normalize_do_work_batch_plan_skips_invalid_candidate_and_uses_later_valid_one():
    plan = normalize_do_work_batch_plan(
        [
            '{"summary": "Broken", "tasks": [{"task_id": "oops"}]}',
            json.dumps(
                {
                    "summary": "Run task 2 next.",
                    "tasks": [{"task_id": 2, "files": ["src/b.py"]}],
                }
            ),
        ]
    )

    assert plan is not None
    assert plan.summary == "Run task 2 next."
    assert plan.tasks[0].task_id == 2


def test_normalize_do_work_batch_plan_ignores_invalid_direct_payload_and_uses_nested_valid_one():
    plan = normalize_do_work_batch_plan(
        {
            "summary": "Broken",
            "tasks": [{"task_id": "oops"}],
            "result": {
                "summary": "Run task 2 next.",
                "tasks": [{"task_id": 2, "files": ["src/b.py"]}],
            },
        }
    )

    assert plan is not None
    assert plan.summary == "Run task 2 next."
    assert plan.tasks[0].task_id == 2


def test_require_concrete_do_work_batch_plan_rejects_unknown_or_duplicate_tasks():
    with pytest.raises(
        ValueError,
        match="do_work batch plan selected unknown or non-ready task id 3",
    ):
        require_concrete_do_work_batch_plan(
            DoWorkBatchPlan(
                summary="bad",
                tasks=[DoWorkBatchTask(task_id=3)],
            ),
            allowed_task_ids={1, 2},
            max_workers=2,
        )

    with pytest.raises(
        ValueError,
        match="do_work batch plan task ids must be unique",
    ):
        require_concrete_do_work_batch_plan(
            DoWorkBatchPlan(
                summary="dup",
                tasks=[DoWorkBatchTask(task_id=1), DoWorkBatchTask(task_id=1)],
            ),
            allowed_task_ids={1, 2},
            max_workers=2,
        )
