"""Optional CrewAI-based planning stage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crewai import Crew

from .crew_defs import (
    build_agents_from_yaml,
    build_crew,
    build_tasks_from_yaml,
    crew_llm,
    delegation_enabled,
    is_hierarchical,
    load_crew_yaml,
    tool_agent_map,
)
from .plan_contract import PlanOutput, normalize_plan_output
from .review_crew import HeadlessInspectTool
from .tools.coder_tool import HeadlessCoderTool


def normalize_plan_crew_output(output: Any) -> PlanOutput:
    """Convert CrewAI's output variants into the Flow's plan contract."""
    return normalize_plan_output(output)


def build_plan_crew(
    *,
    planning_context: str,
    worker_tool: HeadlessCoderTool,
    cwd: str,
    timeout: int,
    model: str | None = None,
    crew_config: dict[str, Any] | None = None,
    config_dir: str | Path | None = None,
) -> Crew:
    """Construct the Planning Crew without invoking it.

    Kept separate from ``run_plan_crew`` so offline tests can assert on
    ``crew.process``, ``crew.manager_llm``, and per-agent ``allow_delegation``
    without requiring a live LLM (only ``crew.kickoff()`` makes network calls).

    Agent/task text comes from ``config/crews/plan/{agents,tasks}.yaml``.
    """

    crew_config = crew_config or {}
    hierarchical = is_hierarchical(crew_config)
    llm = crew_llm(crew_config)
    inspect_tool = HeadlessInspectTool(
        worker_tool=worker_tool,
        cwd=cwd,
        timeout=timeout,
        model=model,
    )
    agents_config, tasks_config = load_crew_yaml("plan", config_dir=config_dir)
    tools_by_agent = tool_agent_map(
        ("researcher", [inspect_tool]),
        ("planner", [inspect_tool]),
    )
    agents = build_agents_from_yaml(
        agents_config,
        llm=llm,
        tools_by_agent=tools_by_agent,
        delegation_agent_keys={"coordinator"},
        allow_delegation=delegation_enabled(crew_config),
    )
    tasks = build_tasks_from_yaml(
        tasks_config,
        agents,
        assign_agents=not hierarchical,
        description_vars={"context": planning_context},
        output_pydantic_by_task={"final_task": PlanOutput},
    )
    return build_crew(agents=agents, tasks=tasks, crew_config=crew_config)


def run_plan_crew(
    *,
    planning_context: str,
    worker_tool: HeadlessCoderTool,
    cwd: str,
    timeout: int,
    model: str | None = None,
    crew_config: dict[str, Any] | None = None,
    config_dir: str | Path | None = None,
) -> PlanOutput:
    """Run the optional Planning Crew (sequential or hierarchical) and normalize its plan."""

    crew = build_plan_crew(
        planning_context=planning_context,
        worker_tool=worker_tool,
        cwd=cwd,
        timeout=timeout,
        model=model,
        crew_config=crew_config,
        config_dir=config_dir,
    )
    return normalize_plan_crew_output(crew.kickoff())
