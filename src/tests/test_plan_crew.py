from __future__ import annotations

from types import SimpleNamespace

import pytest

from crewai_headless_flow.plan_contract import PlanOutput
from crewai_headless_flow.plan_crew import normalize_plan_crew_output


pytestmark = pytest.mark.offline


def test_normalize_plan_crew_output_accepts_pydantic_result():
    crew_output = SimpleNamespace(
        pydantic=PlanOutput(
            spec="Ship feature safely.",
            tasks=[
                {
                    "id": 1,
                    "title": "Add contract",
                    "description": "Create the contract.",
                }
            ],
        )
    )

    plan = normalize_plan_crew_output(crew_output)

    assert plan.spec == "Ship feature safely."
    assert plan.tasks[0].title == "Add contract"


def test_normalize_plan_crew_output_falls_back_to_legacy_tagged_text():
    crew_output = SimpleNamespace(
        pydantic=None,
        json_dict=None,
        raw="""
<spec>
Implement planning.
</spec>

<tasks>
1. Inspect repo
</tasks>
""".strip(),
    )

    plan = normalize_plan_crew_output(crew_output)

    assert plan.spec == "Implement planning."
    assert plan.tasks[0].description == "Inspect repo"
