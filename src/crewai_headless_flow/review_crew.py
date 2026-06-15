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


def _crew_process(crew_config: dict[str, Any]) -> Process:
    process = crew_config.get("process", "sequential")
    if process != "sequential":
        raise ValueError("Review Crew only supports process='sequential'")
    return Process.sequential


def run_review_crew(
    *,
    review_context: str,
    worker_tool: HeadlessCoderTool,
    cwd: str,
    timeout: int,
    model: str | None = None,
    crew_config: dict[str, Any] | None = None,
) -> ReviewCrewDecision:
    """Run the optional sequential Review Crew and normalize its decision."""

    crew_config = crew_config or {}
    llm = _crew_llm(crew_config)
    inspect_tool = HeadlessInspectTool(
        worker_tool=worker_tool,
        cwd=cwd,
        timeout=timeout,
        model=model,
    )

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
    )

    evidence_task = Task(
        description=(
            "Use the read-only inspection tool to gather evidence for this review.\n\n"
            f"{review_context}"
        ),
        expected_output="A concise evidence summary with files, tests, and risks.",
        agent=evidence_agent,
    )
    correctness_task = Task(
        description="Review the evidence for correctness defects.",
        expected_output="Concrete correctness issues, or an explicit no-issues statement.",
        agent=correctness_agent,
        context=[evidence_task],
    )
    test_task = Task(
        description="Review the evidence for missing or weak tests.",
        expected_output="Concrete test coverage issues, or an explicit no-issues statement.",
        agent=test_agent,
        context=[evidence_task],
    )
    scope_task = Task(
        description="Review the evidence for scope or safety problems.",
        expected_output="Concrete scope/safety issues, or an explicit no-issues statement.",
        agent=scope_agent,
        context=[evidence_task],
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
        agent=coordinator,
        context=[correctness_task, test_task, scope_task],
        output_pydantic=ReviewCrewDecision,
    )

    crew = Crew(
        agents=[
            evidence_agent,
            correctness_agent,
            test_agent,
            scope_agent,
            coordinator,
        ],
        tasks=[
            evidence_task,
            correctness_task,
            test_task,
            scope_task,
            decision_task,
        ],
        process=_crew_process(crew_config),
        verbose=False,
    )

    return normalize_review_crew_output(crew.kickoff())
