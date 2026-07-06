"""Optional CrewAI-based review stage.

The Flow remains the state machine. This module only owns the richer review
sub-workflow used when ``stages.review.crew.enabled`` is true.
"""

from __future__ import annotations

import json
from typing import Any

from crewai import Agent, Crew, LLM, Process, Task
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from .review_contract import ReviewDecision, normalize_review_output
from .tools.coder_tool import HeadlessCoderTool


PARSE_FAILURE_ISSUE = "Review Crew output could not be parsed"
ReviewCrewDecision = ReviewDecision


class HeadlessInspectInput(BaseModel):
    """Input schema for read-only headless inspection."""

    prompt: str = Field(..., description="Read-only review prompt to inspect.")


class HeadlessInspectTool(BaseTool):
    """CrewAI tool that only exposes the configured worker's inspect mode."""

    name: str = "headless_inspect"
    description: str = (
        "Runs the configured headless coding worker in read-only inspect mode. "
        "Use it to inspect code, diffs, and tests without modifying files."
    )
    args_schema: type[BaseModel] = HeadlessInspectInput
    worker_tool: Any
    cwd: str
    timeout: int = 300
    model: str | None = None

    def _run(self, prompt: str) -> str:
        result = self.worker_tool.run(
            task=prompt,
            cwd=self.cwd,
            mode="inspect",
            timeout=self.timeout,
            model=self.model,
        )
        return result.summary or result.raw_output or ""


def _fail_closed(issue: str = PARSE_FAILURE_ISSUE) -> ReviewCrewDecision:
    return ReviewCrewDecision(status="revise", issues=[issue], summary=issue)


def _load_json_object(raw: str) -> dict[str, Any] | None:
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start == -1 or end <= start:
        return None
    try:
        loaded = json.loads(raw[start:end])
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def normalize_review_crew_output(output: Any) -> ReviewCrewDecision:
    """Convert CrewAI's output variants into the Flow's review decision."""
    return normalize_review_output(
        output,
        parse_failure_issue=PARSE_FAILURE_ISSUE,
    )


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


def _build_review_agents(
    *, llm: LLM, inspect_tool: HeadlessInspectTool, delegation_enabled: bool
) -> dict[str, Agent]:
    evidence_agent = Agent(
        role="Review Evidence Collector",
        goal="Collect read-only evidence about the recent implementation.",
        backstory=(
            "You inspect repository state and summarize only facts that are "
            "relevant to deciding whether the implementation should pass review."
        ),
        tools=[inspect_tool],
        llm=llm,
        verbose=False,
    )
    correctness_agent = Agent(
        role="Correctness Reviewer",
        goal="Find behavior, edge-case, and integration defects.",
        backstory="You are a rigorous software reviewer focused on correctness.",
        tools=[inspect_tool],
        llm=llm,
        verbose=False,
    )
    test_agent = Agent(
        role="Test Coverage Reviewer",
        goal="Identify missing or weak tests for the requested change.",
        backstory="You review whether tests prove the new behavior and guard regressions.",
        tools=[inspect_tool],
        llm=llm,
        verbose=False,
    )
    scope_agent = Agent(
        role="Scope and Safety Reviewer",
        goal="Check whether the work stayed in scope and preserved safety constraints.",
        backstory="You focus on unintended mutations, broad refactors, and safety regressions.",
        tools=[inspect_tool],
        llm=llm,
        verbose=False,
    )
    coordinator = Agent(
        role="Review Coordinator",
        goal="Merge reviewer findings into one pass-or-revise decision.",
        backstory="You make conservative release decisions from concrete review evidence.",
        llm=llm,
        verbose=False,
        allow_delegation=delegation_enabled,
    )
    return {
        "evidence": evidence_agent,
        "correctness": correctness_agent,
        "test": test_agent,
        "scope": scope_agent,
        "coordinator": coordinator,
    }


def _build_review_tasks(
    agents: dict[str, Agent], review_context: str, *, assign_agents: bool
) -> list[Task]:
    """Build the review task pipeline.

    When ``assign_agents`` is False (hierarchical mode), tasks are built
    without a fixed ``agent=`` so CrewAI's manager actually decides who runs
    each task at runtime instead of following a pre-assigned pipeline.
    """

    def agent_or_none(name: str) -> Agent | None:
        return agents[name] if assign_agents else None

    evidence_task = Task(
        description=(
            "Use the read-only inspection tool to gather evidence for this review.\n\n"
            f"{review_context}"
        ),
        expected_output="A concise evidence summary with files, tests, and risks.",
        agent=agent_or_none("evidence"),
    )
    correctness_task = Task(
        description="Review the evidence for correctness defects.",
        expected_output="Concrete correctness issues, or an explicit no-issues statement.",
        context=[evidence_task],
        agent=agent_or_none("correctness"),
    )
    test_task = Task(
        description="Review the evidence for missing or weak tests.",
        expected_output="Concrete test coverage issues, or an explicit no-issues statement.",
        context=[evidence_task],
        agent=agent_or_none("test"),
    )
    scope_task = Task(
        description="Review the evidence for scope or safety problems.",
        expected_output="Concrete scope/safety issues, or an explicit no-issues statement.",
        context=[evidence_task],
        agent=agent_or_none("scope"),
    )
    decision_task = Task(
        description=(
            "Merge the reviewer outputs into a single decision. Use status='pass' "
            "only if there are no concrete issues. Use status='revise' when any "
            "correctness, test coverage, scope, or safety issue remains."
        ),
        expected_output=(
            "A JSON object with status ('pass' or 'revise'), issues, and summary."
        ),
        context=[correctness_task, test_task, scope_task],
        output_pydantic=ReviewCrewDecision,
        agent=agent_or_none("coordinator"),
    )
    return [evidence_task, correctness_task, test_task, scope_task, decision_task]


def build_review_crew(
    *,
    review_context: str,
    worker_tool: HeadlessCoderTool,
    cwd: str,
    timeout: int,
    model: str | None = None,
    crew_config: dict[str, Any] | None = None,
) -> Crew:
    """Construct the Review Crew without invoking it.

    Kept separate from ``run_review_crew`` so offline tests can assert on
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

    agents = _build_review_agents(
        llm=llm,
        inspect_tool=inspect_tool,
        delegation_enabled=_delegation_enabled(crew_config),
    )
    tasks = _build_review_tasks(agents, review_context, assign_agents=not hierarchical)

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


def run_review_crew(
    *,
    review_context: str,
    worker_tool: HeadlessCoderTool,
    cwd: str,
    timeout: int,
    model: str | None = None,
    crew_config: dict[str, Any] | None = None,
) -> ReviewCrewDecision:
    """Run the optional Review Crew (sequential or hierarchical) and normalize its decision."""

    crew = build_review_crew(
        review_context=review_context,
        worker_tool=worker_tool,
        cwd=cwd,
        timeout=timeout,
        model=model,
        crew_config=crew_config,
    )
    return normalize_review_crew_output(crew.kickoff())
