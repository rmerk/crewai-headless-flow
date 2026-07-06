from __future__ import annotations

from types import SimpleNamespace

import pytest
from crewai import Process

from crewai_headless_flow.plan_contract import PlanOutput
from crewai_headless_flow.plan_crew import build_plan_crew, normalize_plan_crew_output
from crewai_headless_flow.workers.base import CoderResult


pytestmark = pytest.mark.offline


class RecordingWorkerTool:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return CoderResult(
            summary="inspected",
            raw_output="inspect details",
            exit_code=0,
        )


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


def _build_plan_crew(**crew_config_overrides):
    return build_plan_crew(
        planning_context="Plan the requested feature.",
        worker_tool=RecordingWorkerTool(),
        cwd="/tmp/repo",
        timeout=17,
        crew_config=crew_config_overrides,
    )


def test_build_plan_crew_defaults_to_sequential_without_delegation():
    crew = _build_plan_crew()

    assert crew.process == Process.sequential
    coordinator = next(a for a in crew.agents if a.role == "Planning Coordinator")
    assert coordinator.allow_delegation is False
    assert all(task.agent is not None for task in crew.tasks)


def test_build_plan_crew_sequential_delegation_enabled_only_on_coordinator():
    crew = _build_plan_crew(process="sequential", delegation={"enabled": True})

    assert crew.process == Process.sequential
    for agent in crew.agents:
        if agent.role == "Planning Coordinator":
            assert agent.allow_delegation is True
        else:
            assert agent.allow_delegation is False


def test_build_plan_crew_hierarchical_uses_manager_llm_and_unassigned_tasks():
    crew = _build_plan_crew(
        process="hierarchical",
        manager={"llm": {"model": "gpt-4o"}},
    )

    assert crew.process == Process.hierarchical
    assert crew.manager_llm is not None
    assert crew.manager_llm.model == "gpt-4o"
    assert all(task.agent is None for task in crew.tasks)


def test_build_plan_crew_hierarchical_manager_llm_falls_back_to_crew_llm():
    crew = _build_plan_crew(
        process="hierarchical",
        llm={"model": "custom-fallback-model"},
    )

    assert crew.manager_llm.model == "custom-fallback-model"


def test_build_plan_crew_hierarchical_ignores_delegation_flag():
    crew = _build_plan_crew(process="hierarchical", delegation={"enabled": True})

    coordinator = next(a for a in crew.agents if a.role == "Planning Coordinator")
    assert coordinator.allow_delegation is False
