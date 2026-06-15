"""Optional CrewAI-based implementation stage."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from crewai import Agent, Crew, LLM, Process, Task
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from .do_work_contract import (
    DoWorkExecutionPlan,
    DoWorkSubtask,
    normalize_do_work_execution_plan,
    require_concrete_do_work_execution_plan,
)
from .review_contract import (
    ReviewDecision,
    fail_closed_review_decision,
    normalize_review_output,
)
from .review_crew import HeadlessInspectTool
from .tools.coder_tool import HeadlessCoderTool
from .workers.base import CoderResult


PARSE_FAILURE_ISSUE = "Implementation Crew output could not be parsed"
DEFAULT_MAX_ROUNDS = 2
DEFAULT_MAX_SUBTASKS = 4


class HeadlessEditInput(BaseModel):
    """Input schema for bounded edit-mode execution."""

    prompt: str = Field(..., description="Implementation prompt to execute.")


class HeadlessEditTool(BaseTool):
    """CrewAI tool that exposes the configured worker's edit mode."""

    name: str = "headless_edit"
    description: str = (
        "Runs the configured headless coding worker in edit mode. "
        "Use it to implement the assigned change in the current repository."
    )
    args_schema: type[BaseModel] = HeadlessEditInput
    worker_tool: Any
    cwd: str
    timeout: int = 300
    model: str | None = None
    results: list[CoderResult] = Field(default_factory=list)

    def _run(self, prompt: str) -> str:
        result = self.worker_tool.run(
            task=prompt,
            cwd=self.cwd,
            mode="edit",
            timeout=self.timeout,
            model=self.model,
        )
        self.results.append(result)
        return result.summary or result.raw_output or ""


def normalize_do_work_crew_output(output: Any) -> ReviewDecision:
    """Convert CrewAI's output variants into a task-local pass/revise decision."""

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
        raise ValueError("Implementation Crew only supports process='sequential'")
    return Process.sequential


def run_do_work_crew(
    *,
    task_prompt: str,
    worker_tool: HeadlessCoderTool,
    cwd: str,
    timeout: int,
    model: str | None = None,
    crew_config: dict[str, Any] | None = None,
    round_observer: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[CoderResult, ReviewDecision]:
    """Run optional Implementation Crew and return edit + decision."""

    crew_config = crew_config or {}
    subtasks, used_decomposition = _planned_subtasks(
        task_prompt=task_prompt,
        worker_tool=worker_tool,
        cwd=cwd,
        timeout=timeout,
        model=model,
        crew_config=crew_config,
    )

    last_result = CoderResult(
        summary="Implementation Crew did not run",
        raw_output="",
        exit_code=1,
        error="Implementation Crew did not run",
    )
    last_decision = fail_closed_review_decision(
        "Implementation Crew did not produce a decision"
    )
    successful_summaries: list[str] = []
    raw_outputs: list[str] = []

    for subtask in subtasks:
        last_result, last_decision = _run_prompt_with_rounds(
            task_prompt=(
                _subtask_prompt(task_prompt, subtask, len(subtasks))
                if used_decomposition
                else task_prompt
            ),
            worker_tool=worker_tool,
            cwd=cwd,
            timeout=timeout,
            model=model,
            crew_config=crew_config,
            subtask=subtask if used_decomposition else None,
            round_observer=round_observer,
        )
        raw_outputs.append(last_result.raw_output)

        if not last_result.success or last_decision.status != "pass":
            if used_decomposition:
                return _annotate_failed_subtask(last_result, last_decision, subtask)
            return last_result, last_decision

        summary = last_decision.summary or last_result.summary or last_result.raw_output
        if used_decomposition:
            successful_summaries.append(f"Subtask {subtask.id} complete: {summary}")

    if not used_decomposition:
        return last_result, last_decision

    combined_summary = "\n".join(successful_summaries).strip()
    return (
        CoderResult(
            summary=combined_summary or last_result.summary,
            changed_files=last_result.changed_files,
            tests_passed=last_result.tests_passed,
            raw_output="\n\n".join(block for block in raw_outputs if block.strip()),
            exit_code=last_result.exit_code,
            error=last_result.error,
        ),
        ReviewDecision(
            status="pass",
            issues=[],
            summary=combined_summary or last_decision.summary,
        ),
    )


def _run_prompt_with_rounds(
    *,
    task_prompt: str,
    worker_tool: HeadlessCoderTool,
    cwd: str,
    timeout: int,
    model: str | None = None,
    crew_config: dict[str, Any] | None = None,
    subtask: DoWorkSubtask | None = None,
    round_observer: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[CoderResult, ReviewDecision]:
    crew_config = crew_config or {}
    feedback_block: str | None = None
    last_result = CoderResult(
        summary="Implementation Crew did not run",
        raw_output="",
        exit_code=1,
        error="Implementation Crew did not run",
    )
    last_decision = fail_closed_review_decision(
        "Implementation Crew did not produce a decision"
    )

    for round_number in range(1, _max_rounds(crew_config) + 1):
        round_prompt = _round_prompt(
            task_prompt=task_prompt,
            round_number=round_number,
            feedback_block=feedback_block,
        )
        last_result, last_decision = _run_do_work_crew_round(
            task_prompt=round_prompt,
            worker_tool=worker_tool,
            cwd=cwd,
            timeout=timeout,
            model=model,
            crew_config=crew_config,
        )
        if round_observer is not None:
            round_observer(
                {
                    "round": round_number,
                    "subtask_id": subtask.id if subtask else None,
                    "subtask_title": subtask.title if subtask else None,
                    "decision_status": last_decision.status,
                    "decision_summary": last_decision.summary,
                    "decision_issues": list(last_decision.issues),
                    "result_summary": last_result.summary,
                    "result_error": last_result.error,
                }
            )
        if last_decision.status == "pass":
            return last_result, last_decision
        feedback_block = _feedback_block(last_decision, round_number)

    return last_result, last_decision


def _run_do_work_crew_round(
    *,
    task_prompt: str,
    worker_tool: HeadlessCoderTool,
    cwd: str,
    timeout: int,
    model: str | None = None,
    crew_config: dict[str, Any] | None = None,
) -> tuple[CoderResult, ReviewDecision]:
    """Run a single Implementation Crew round and return edit + decision."""

    crew_config = crew_config or {}
    llm = _crew_llm(crew_config)
    inspect_tool = HeadlessInspectTool(
        worker_tool=worker_tool,
        cwd=cwd,
        timeout=timeout,
        model=model,
    )
    edit_tool = HeadlessEditTool(
        worker_tool=worker_tool,
        cwd=cwd,
        timeout=timeout,
        model=model,
    )

    evidence_agent = Agent(
        role="Implementation Evidence Collector",
        goal="Gather grounded repository context for current implementation slice.",
        backstory=(
            "You inspect repository state and summarize only facts that are "
            "relevant to safely implementing assigned slice."
        ),
        tools=[inspect_tool],
        llm=llm,
        verbose=False,
    )
    implementer_agent = Agent(
        role="Task Implementer",
        goal="Implement assigned slice within stated boundaries.",
        backstory="You make focused, minimal changes that satisfy current slice.",
        tools=[edit_tool],
        llm=llm,
        verbose=False,
    )
    verifier_agent = Agent(
        role="Task Verifier",
        goal="Check whether implementation appears complete and on-scope.",
        backstory="You look for missing behavior, weak verification, and scope drift.",
        tools=[inspect_tool],
        llm=llm,
        verbose=False,
    )
    coordinator = Agent(
        role="Implementation Coordinator",
        goal="Decide whether current slice is ready to progress.",
        backstory="You make conservative pass-or-revise calls from concrete evidence.",
        llm=llm,
        verbose=False,
    )

    evidence_task = Task(
        description=(
            "Use read-only inspect tool to gather implementation context.\n\n"
            f"{task_prompt}"
        ),
        expected_output=(
            "Concise evidence summary with relevant files, dependencies, and "
            "verification hooks."
        ),
        agent=evidence_agent,
    )
    implementation_task = Task(
        description=(
            "Use edit tool to implement current slice. Stay scoped to current "
            "slice and summarize what changed."
        ),
        expected_output="Concise implementation summary from edit run.",
        agent=implementer_agent,
        context=[evidence_task],
    )
    verification_task = Task(
        description=(
            "Use inspect tool to verify whether implementation appears to "
            "satisfy current slice. Call out concrete remaining issues if any."
        ),
        expected_output="Concrete remaining issues, or an explicit no-issues statement.",
        agent=verifier_agent,
        context=[evidence_task, implementation_task],
    )
    decision_task = Task(
        description=(
            "Merge implementation and verification outputs into single decision. "
            "Use status='pass' only if no concrete issue remains. Use "
            "status='revise' when missing behavior, missing verification, "
            "or scope drift remains."
        ),
        expected_output="A JSON object with status, issues, and summary.",
        agent=coordinator,
        context=[implementation_task, verification_task],
        output_pydantic=ReviewDecision,
    )

    crew = Crew(
        agents=[evidence_agent, implementer_agent, verifier_agent, coordinator],
        tasks=[evidence_task, implementation_task, verification_task, decision_task],
        process=_crew_process(crew_config),
        verbose=False,
    )

    decision = normalize_do_work_crew_output(crew.kickoff())
    if not edit_tool.results:
        issue = "Implementation Crew did not invoke the edit tool"
        return (
            CoderResult(
                summary=issue,
                raw_output="",
                exit_code=1,
                error=issue,
            ),
            fail_closed_review_decision(issue),
        )

    return edit_tool.results[-1], decision


def _planned_subtasks(
    *,
    task_prompt: str,
    worker_tool: HeadlessCoderTool,
    cwd: str,
    timeout: int,
    model: str | None,
    crew_config: dict[str, Any],
) -> tuple[list[DoWorkSubtask], bool]:
    if not _decomposition_enabled(crew_config):
        return [_synthetic_subtask(task_prompt)], False

    max_subtasks = _max_subtasks(crew_config)
    try:
        plan = _run_do_work_decomposition_crew(
            task_prompt=task_prompt,
            worker_tool=worker_tool,
            cwd=cwd,
            timeout=timeout,
            model=model,
            crew_config=crew_config,
        )
        if plan is None:
            raise ValueError("decomposition output could not be parsed")
        return (
            require_concrete_do_work_execution_plan(
                plan,
                max_subtasks=max_subtasks,
            ).subtasks,
            True,
        )
    except Exception:
        return [_synthetic_subtask(task_prompt)], False


def _run_do_work_decomposition_crew(
    *,
    task_prompt: str,
    worker_tool: HeadlessCoderTool,
    cwd: str,
    timeout: int,
    model: str | None,
    crew_config: dict[str, Any],
) -> DoWorkExecutionPlan | None:
    llm = _crew_llm(crew_config)
    inspect_tool = HeadlessInspectTool(
        worker_tool=worker_tool,
        cwd=cwd,
        timeout=timeout,
        model=model,
    )
    max_subtasks = _max_subtasks(crew_config)

    researcher = Agent(
        role="Implementation Researcher",
        goal="Gather facts needed to split current task into safe execution slices.",
        backstory="You inspect repo state and surface only grounded execution constraints.",
        tools=[inspect_tool],
        llm=llm,
        verbose=False,
    )
    decomposer = Agent(
        role="Task Decomposer",
        goal="Break current task into small ordered execution slices.",
        backstory="You prefer few slices and avoid decomposition when one slice is enough.",
        llm=llm,
        verbose=False,
    )
    validator = Agent(
        role="Decomposition Validator",
        goal="Catch weak slice boundaries, missing file hints, and extra complexity.",
        backstory="You keep execution plans conservative and grounded.",
        llm=llm,
        verbose=False,
    )

    research_task = Task(
        description=(
            "Use read-only inspect tool to gather decomposition context.\n\n"
            f"{task_prompt}"
        ),
        expected_output=(
            "Concise research summary with likely files, dependencies, and "
            "verification hooks for current task."
        ),
        agent=researcher,
    )
    draft_task = Task(
        description=(
            "Using research summary, break current task into at most "
            f"{max_subtasks} ordered execution slices. Use one slice when task is "
            "already small. Each slice should have a short title, concrete "
            "description, likely files, and verification checks."
        ),
        expected_output="Draft ordered slice list.",
        agent=decomposer,
        context=[research_task],
    )
    final_task = Task(
        description=(
            "Return final structured execution plan. Keep it conservative and "
            "ordered. Prefer fewer slices. If uncertainty is high, still return "
            "one concrete slice rather than inventing extra structure."
        ),
        expected_output="A JSON object with summary and subtasks.",
        agent=validator,
        context=[research_task, draft_task],
        output_pydantic=DoWorkExecutionPlan,
    )

    crew = Crew(
        agents=[researcher, decomposer, validator],
        tasks=[research_task, draft_task, final_task],
        process=_crew_process(crew_config),
        verbose=False,
    )

    return normalize_do_work_execution_plan(crew.kickoff())


def _annotate_failed_subtask(
    result: CoderResult,
    decision: ReviewDecision,
    subtask: DoWorkSubtask,
) -> tuple[CoderResult, ReviewDecision]:
    prefix = f"Subtask {subtask.id} ({subtask.title})"
    annotated_result = result
    if result.error:
        annotated_result = CoderResult(
            summary=result.summary,
            changed_files=result.changed_files,
            tests_passed=result.tests_passed,
            raw_output=result.raw_output,
            exit_code=result.exit_code,
            error=f"{prefix} failed: {result.error}",
        )
    annotated_decision = decision
    if decision.status != "pass":
        issues = decision.issues or [decision.summary]
        annotated_decision = ReviewDecision(
            status=decision.status,
            issues=[f"{prefix}: {issue}" for issue in issues],
            summary=f"{prefix} requested revision: {decision.summary}",
            task_hints=decision.task_hints,
        )
    return annotated_result, annotated_decision


def _synthetic_subtask(task_prompt: str) -> DoWorkSubtask:
    return DoWorkSubtask(
        id=1,
        title="Main task",
        description=task_prompt,
    )


def _subtask_prompt(
    task_prompt: str,
    subtask: DoWorkSubtask,
    total_subtasks: int,
) -> str:
    files = "\n".join(f"- {path}" for path in subtask.files)
    verification = "\n".join(f"- {item}" for item in subtask.verification)
    return (
        f"{task_prompt}\n\n"
        f"Implementation slice {subtask.id}/{total_subtasks}:\n"
        f"- Title: {subtask.title}\n"
        f"- Description: {subtask.description}\n"
        f"Slice files likely touched:\n{files or '- None provided'}\n"
        f"Slice verification:\n{verification or '- None provided'}\n\n"
        "Stay scoped to this slice while preserving repo coherence. Report what "
        "changed and whether slice verification now passes."
    )


def _feedback_block(decision: ReviewDecision, round_number: int) -> str:
    issues = decision.issues or [decision.summary]
    lines = [
        f"Previous implementation round {round_number} requested revision.",
        f"Summary: {decision.summary}",
        "Address these concrete issues before claiming completion:",
        *[f"- {issue}" for issue in issues],
    ]
    return "\n".join(lines)


def _round_prompt(
    *,
    task_prompt: str,
    round_number: int,
    feedback_block: str | None,
) -> str:
    if not feedback_block:
        return task_prompt
    return (
        f"{task_prompt}\n\n"
        f"Implementation crew round: {round_number}\n"
        f"{feedback_block}\n"
    )


def _max_rounds(crew_config: dict[str, Any]) -> int:
    raw = crew_config.get("max_rounds", DEFAULT_MAX_ROUNDS)
    value = int(raw)
    if value < 1:
        raise ValueError("Implementation Crew max_rounds must be at least 1")
    return value


def _decomposition_enabled(crew_config: dict[str, Any]) -> bool:
    decomposition_cfg = crew_config.get("decomposition", {}) or {}
    return bool(decomposition_cfg.get("enabled", False))


def _max_subtasks(crew_config: dict[str, Any]) -> int:
    decomposition_cfg = crew_config.get("decomposition", {}) or {}
    raw = decomposition_cfg.get("max_subtasks", DEFAULT_MAX_SUBTASKS)
    value = int(raw)
    if value < 1:
        raise ValueError(
            "Implementation Crew decomposition max_subtasks must be at least 1"
        )
    return value
