from __future__ import annotations

from types import SimpleNamespace

import pytest

from crewai_headless_flow.plan_contract import (
    PlanOutput,
    PlanTask,
    normalize_plan_output,
    plan_tasks_to_state_items,
    render_plan_markdown,
)


pytestmark = pytest.mark.offline


def test_normalize_plan_output_accepts_pydantic_result():
    output = SimpleNamespace(
        pydantic=PlanOutput(
            spec="Ship feature safely.",
            tasks=[
                PlanTask(
                    id=1,
                    title="Add contract",
                    description="Create structured contract.",
                    acceptance_criteria=["Model exists"],
                    verification=["pytest -q"],
                    dependencies=[],
                    files=["src/contract.py"],
                    estimated_scope="S",
                )
            ],
        )
    )

    plan = normalize_plan_output(output)

    assert plan.spec == "Ship feature safely."
    assert len(plan.tasks) == 1
    assert plan.tasks[0].title == "Add contract"


def test_normalize_plan_output_falls_back_to_legacy_tagged_text():
    raw = """
<spec>
Implement structured planning.
</spec>

<tasks>
1. Add plan contract
2. Populate state.tasks
</tasks>
""".strip()

    plan = normalize_plan_output(raw)

    assert plan.spec == "Implement structured planning."
    assert [task.description for task in plan.tasks] == [
        "Add plan contract",
        "Populate state.tasks",
    ]


def test_normalize_plan_output_skips_invalid_candidate_and_uses_later_valid_one():
    plan = normalize_plan_output(
        [
            '{"spec": "Broken", "tasks": [{"id": "oops", "description": "Bad"}]}',
            """
<spec>
Implement structured planning.
</spec>

<tasks>
1. Add plan contract
</tasks>
""".strip(),
        ]
    )

    assert plan.spec == "Implement structured planning."
    assert [task.id for task in plan.tasks] == [1]


def test_normalize_plan_output_ignores_invalid_direct_payload_and_uses_nested_valid_one():
    plan = normalize_plan_output(
        {
            "spec": "Broken",
            "tasks": [{"id": "oops", "description": "Bad"}],
            "result": {
                "spec": "Ship feature safely.",
                "tasks": [
                    {
                        "id": 1,
                        "title": "Add contract",
                        "description": "Create structured contract.",
                    }
                ],
            },
        }
    )

    assert plan.spec == "Ship feature safely."
    assert plan.tasks[0].id == 1


def test_render_plan_markdown_includes_task_details():
    plan = PlanOutput(
        spec="Implement structured planning.",
        tasks=[
            PlanTask(
                id=1,
                title="Add plan contract",
                description="Create typed planning models.",
                acceptance_criteria=["Model parses"],
                verification=["uv run pytest -q"],
                dependencies=[],
                files=["src/crewai_headless_flow/plan_contract.py"],
                estimated_scope="S",
            )
        ],
    )

    rendered = render_plan_markdown(plan)

    assert "<spec>" in rendered
    assert "1. Add plan contract" in rendered
    assert "Acceptance criteria:" in rendered
    assert "Verification:" in rendered


def test_plan_tasks_to_state_items_preserves_parallelization_metadata():
    tasks = [
        PlanTask(
            id=2,
            title="Wire flow",
            description="Populate state tasks.",
            acceptance_criteria=["Tasks present"],
            verification=["pytest"],
            dependencies=[1],
            files=["src/crewai_headless_flow/flow.py"],
            estimated_scope="M",
        )
    ]

    state_tasks = plan_tasks_to_state_items(tasks)

    assert state_tasks[0].title == "Wire flow"
    assert state_tasks[0].dependencies == [1]
    assert state_tasks[0].verification == ["pytest"]
