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


def _crew_process(crew_config: dict[str, Any]) -> Process:
    process = crew_config.get("process", "sequential")
    if process != "sequential":
        raise ValueError("Planning Crew only supports process='sequential'")
    return Process.sequential


def run_plan_crew(
    *,
    planning_context: str,
    worker_tool: HeadlessCoderTool,
    cwd: str,
    timeout: int,
    model: str | None = None,
    crew_config: dict[str, Any] | None = None,
) -> PlanOutput:
    """Run the optional sequential Planning Crew and normalize its plan."""

    crew_config = crew_config or {}
    llm = _crew_llm(crew_config)
    inspect_tool = HeadlessInspectTool(
        worker_tool=worker_tool,
        cwd=cwd,
        timeout=timeout,
        model=model,
    )

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
    )

    research_task = Task(
        description=(
            "Use the read-only inspection tool to gather planning context.\n\n"
            f"{planning_context}"
        ),
        expected_output=(
            "A concise research summary covering likely files, dependencies, "
            "constraints, and verification hooks."
        ),
        agent=researcher,
    )
    draft_task = Task(
        description=(
            "Using the research summary, draft an implementation plan with a concise "
            "spec and small vertical-slice tasks."
        ),
        expected_output="A draft plan with spec text and ordered tasks.",
        agent=planner,
        context=[research_task],
    )
    validation_task = Task(
        description=(
            "Review the draft plan for missing files, weak verification, bad "
            "dependencies, or scope drift. Call out concrete fixes."
        ),
        expected_output=("Concrete plan issues or an explicit no-issues statement."),
        agent=validator,
        context=[research_task, draft_task],
    )
    final_task = Task(
        description=(
            "Produce the final structured implementation plan. Incorporate any "
            "validation feedback. Return a single plan object with a concise spec "
            "and ordered tasks that include acceptance criteria, verification, "
            "dependencies, likely files, and estimated scope."
        ),
        expected_output="A JSON object with spec and tasks.",
        agent=coordinator,
        context=[research_task, draft_task, validation_task],
        output_pydantic=PlanOutput,
    )

    crew = Crew(
        agents=[researcher, planner, validator, coordinator],
        tasks=[research_task, draft_task, validation_task, final_task],
        process=_crew_process(crew_config),
        verbose=False,
    )

    return normalize_plan_crew_output(crew.kickoff())
