"""Optional CrewAI-based planning stage."""

from __future__ import annotations

from typing import Any

from crewai import Agent, Crew, LLM, Process, Task

from .plan_contract import PlanOutput, normalize_plan_output
from .review_crew import HeadlessInspectTool
from .tools.coder_tool import HeadlessCoderTool


def normalize_plan_crew_output(output: Any) -> PlanOutput:
    """Convert CrewAI's output variants into the Flow's plan contract."""
    return normalize_plan_output(output)


def _crew_llm(crew_config: dict[str, Any]) -> LLM:
    llm_cfg = crew_config.get("llm", {}) or {}
    return LLM(
        model=llm_cfg.get("model", "ollama/llama3.2"),
        base_url=llm_cfg.get("base_url", "http://localhost:11434"),
        temperature=llm_cfg.get("temperature", 0.2),
    )


def _is_hierarchical(crew_config: dict[str, Any]) -> bool:
    return crew_config.get("process", "sequential") == "hierarchical"


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _manager_llm(crew_config: dict[str, Any]) -> LLM:
    """LLM for the auto-created manager agent (hierarchical mode only).

    Falls back to the crew's own ``llm`` block for any field left unset, so a
    minimal ``manager.llm.model`` override is enough to point the manager at
    a stronger tool-calling model while reusing the crew's base_url/temperature.
    ``None`` (not just "missing key") is treated as unset so an explicit
    ``temperature: 0.0`` override is never mistaken for "fall back".
    """
    fallback_cfg = crew_config.get("llm", {}) or {}
    manager_cfg = (crew_config.get("manager", {}) or {}).get("llm", {}) or {}
    return LLM(
        model=_first_not_none(
            manager_cfg.get("model"), fallback_cfg.get("model"), "ollama/llama3.2"
        ),
        base_url=_first_not_none(
            manager_cfg.get("base_url"),
            fallback_cfg.get("base_url"),
            "http://localhost:11434",
        ),
        temperature=_first_not_none(
            manager_cfg.get("temperature"), fallback_cfg.get("temperature"), 0.2
        ),
    )


def _delegation_enabled(crew_config: dict[str, Any]) -> bool:
    """Sequential-only: whether the coordinator agent gets allow_delegation=True.

    Hierarchical mode always routes execution through CrewAI's auto-created
    manager, so this flag is only meaningful under Process.sequential.
    """
    if _is_hierarchical(crew_config):
        return False
    delegation_cfg = crew_config.get("delegation", {}) or {}
    return bool(delegation_cfg.get("enabled", False))


def _build_plan_agents(
    *, llm: LLM, inspect_tool: HeadlessInspectTool, delegation_enabled: bool
) -> dict[str, Agent]:
    researcher = Agent(
        role="Repository Researcher",
        goal="Gather grounded repository context for planning.",
        backstory=(
            "You inspect repository state and summarize only facts that are "
            "relevant to building a concrete implementation plan."
        ),
        tools=[inspect_tool],
        llm=llm,
        verbose=False,
    )
    planner = Agent(
        role="Implementation Planner",
        goal="Draft a concrete implementation plan from grounded evidence.",
        backstory="You break requests into scoped, testable tasks.",
        tools=[inspect_tool],
        llm=llm,
        verbose=False,
    )
    validator = Agent(
        role="Plan Validator",
        goal="Catch missing dependencies, files, verification steps, and scope drift.",
        backstory="You strengthen plans before they are executed.",
        llm=llm,
        verbose=False,
    )
    coordinator = Agent(
        role="Planning Coordinator",
        goal="Produce the final structured implementation plan.",
        backstory="You reconcile research, planning, and validation into one plan.",
        llm=llm,
        verbose=False,
        allow_delegation=delegation_enabled,
    )
    return {
        "researcher": researcher,
        "planner": planner,
        "validator": validator,
        "coordinator": coordinator,
    }


def _build_plan_tasks(
    agents: dict[str, Agent], planning_context: str, *, assign_agents: bool
) -> list[Task]:
    """Build the planning task pipeline.

    When ``assign_agents`` is False (hierarchical mode), tasks are built
    without a fixed ``agent=`` so CrewAI's manager actually decides who runs
    each task at runtime instead of following a pre-assigned pipeline.
    """

    def agent_or_none(name: str) -> Agent | None:
        return agents[name] if assign_agents else None

    research_task = Task(
        description=(
            "Use the read-only inspection tool to gather planning context.\n\n"
            f"{planning_context}"
        ),
        expected_output=(
            "A concise research summary covering likely files, dependencies, "
            "constraints, and verification hooks."
        ),
        agent=agent_or_none("researcher"),
    )
    draft_task = Task(
        description=(
            "Using the research summary, draft an implementation plan with a concise "
            "spec and small vertical-slice tasks."
        ),
        expected_output="A draft plan with spec text and ordered tasks.",
        context=[research_task],
        agent=agent_or_none("planner"),
    )
    validation_task = Task(
        description=(
            "Review the draft plan for missing files, weak verification, bad "
            "dependencies, or scope drift. Call out concrete fixes."
        ),
        expected_output=("Concrete plan issues or an explicit no-issues statement."),
        context=[research_task, draft_task],
        agent=agent_or_none("validator"),
    )
    final_task = Task(
        description=(
            "Produce the final structured implementation plan. Incorporate any "
            "validation feedback. Return a single plan object with a concise spec "
            "and ordered tasks that include acceptance criteria, verification, "
            "dependencies, likely files, and estimated scope."
        ),
        expected_output="A JSON object with spec and tasks.",
        context=[research_task, draft_task, validation_task],
        output_pydantic=PlanOutput,
        agent=agent_or_none("coordinator"),
    )
    return [research_task, draft_task, validation_task, final_task]


def build_plan_crew(
    *,
    planning_context: str,
    worker_tool: HeadlessCoderTool,
    cwd: str,
    timeout: int,
    model: str | None = None,
    crew_config: dict[str, Any] | None = None,
) -> Crew:
    """Construct the Planning Crew without invoking it.

    Kept separate from ``run_plan_crew`` so offline tests can assert on
    ``crew.process``, ``crew.manager_llm``, and per-agent ``allow_delegation``
    without requiring a live LLM (only ``crew.kickoff()`` makes network calls).
    """

    crew_config = crew_config or {}
    hierarchical = _is_hierarchical(crew_config)
    llm = _crew_llm(crew_config)
    inspect_tool = HeadlessInspectTool(
        worker_tool=worker_tool,
        cwd=cwd,
        timeout=timeout,
        model=model,
    )

    agents = _build_plan_agents(
        llm=llm,
        inspect_tool=inspect_tool,
        delegation_enabled=_delegation_enabled(crew_config),
    )
    tasks = _build_plan_tasks(agents, planning_context, assign_agents=not hierarchical)

    if hierarchical:
        return Crew(
            agents=list(agents.values()),
            tasks=tasks,
            process=Process.hierarchical,
            manager_llm=_manager_llm(crew_config),
            verbose=False,
        )

    return Crew(
        agents=list(agents.values()),
        tasks=tasks,
        process=Process.sequential,
        verbose=False,
    )


def run_plan_crew(
    *,
    planning_context: str,
    worker_tool: HeadlessCoderTool,
    cwd: str,
    timeout: int,
    model: str | None = None,
    crew_config: dict[str, Any] | None = None,
) -> PlanOutput:
    """Run the optional Planning Crew (sequential or hierarchical) and normalize its plan."""

    crew = build_plan_crew(
        planning_context=planning_context,
        worker_tool=worker_tool,
        cwd=cwd,
        timeout=timeout,
        model=model,
        crew_config=crew_config,
    )
    return normalize_plan_crew_output(crew.kickoff())
