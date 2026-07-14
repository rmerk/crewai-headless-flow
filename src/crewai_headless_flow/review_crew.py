"""Optional CrewAI-based review stage.

The Flow remains the state machine. This module only owns the richer review
sub-workflow used when ``stages.review.crew.enabled`` is true.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crewai import Crew
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from .crew_defs import (
    build_agents_from_yaml,
    build_crew,
    build_tasks_from_yaml,
    crew_llm,
    delegation_enabled,
    is_hierarchical,
    load_crew_yaml,
)
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


def normalize_review_crew_output(output: Any) -> ReviewCrewDecision:
    """Convert CrewAI's output variants into the Flow's review decision."""
    return normalize_review_output(
        output,
        parse_failure_issue=PARSE_FAILURE_ISSUE,
    )


def build_review_crew(
    *,
    review_context: str,
    worker_tool: HeadlessCoderTool,
    cwd: str,
    timeout: int,
    model: str | None = None,
    crew_config: dict[str, Any] | None = None,
    config_dir: str | Path | None = None,
) -> Crew:
    """Construct the Review Crew without invoking it.

    Kept separate from ``run_review_crew`` so offline tests can assert on
    ``crew.process``, ``crew.manager_llm``, and per-agent ``allow_delegation``
    without requiring a live LLM (only ``crew.kickoff()`` makes network calls).

    Agent/task text comes from ``config/crews/review/{agents,tasks}.yaml``.
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
    agents_config, tasks_config = load_crew_yaml("review", config_dir=config_dir)
    agents = build_agents_from_yaml(
        agents_config,
        llm=llm,
        tools_by_agent={
            "evidence": [inspect_tool],
            "correctness": [inspect_tool],
            "test": [inspect_tool],
            "scope": [inspect_tool],
        },
        delegation_agent_keys={"coordinator"},
        allow_delegation=delegation_enabled(crew_config),
    )
    tasks = build_tasks_from_yaml(
        tasks_config,
        agents,
        assign_agents=not hierarchical,
        description_vars={"context": review_context},
        output_pydantic_by_task={"decision_task": ReviewCrewDecision},
    )
    return build_crew(agents=agents, tasks=tasks, crew_config=crew_config)


def run_review_crew(
    *,
    review_context: str,
    worker_tool: HeadlessCoderTool,
    cwd: str,
    timeout: int,
    model: str | None = None,
    crew_config: dict[str, Any] | None = None,
    config_dir: str | Path | None = None,
) -> ReviewCrewDecision:
    """Run the optional Review Crew (sequential or hierarchical) and normalize its decision."""

    crew = build_review_crew(
        review_context=review_context,
        worker_tool=worker_tool,
        cwd=cwd,
        timeout=timeout,
        model=model,
        crew_config=crew_config,
        config_dir=config_dir,
    )
    return normalize_review_crew_output(crew.kickoff())
