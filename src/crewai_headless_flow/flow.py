"""
The main reusable CrewAI Flow for multi-agent headless coding with pluggable workers.

Topology (as specified):
- @start plan          → orchestration LLM + planning + spec skills
- @listen do_work      → configured worker (edit mode) + implementation skill
- @router review       → configured worker (inspect mode) + review + doubt skills → "pass" | "revise"
- @listen("revise")    → bounded loop back to do_work
- @listen("pass")      → finalize with documentation skill
"""

from __future__ import annotations

import json
from typing import Literal

from crewai import Agent, LLM, Task
from crewai.flow.flow import Flow, listen, router, start

from .config import FlowConfig, get_default_config, print_stage_mapping
from .review_crew import ReviewCrewDecision, run_review_crew
from .skills.loader import get_default_loader
from .state import FlowState
from .tools.coder_tool import HeadlessCoderTool
from .workers import ClaudeAdapter, CodexAdapter, GrokAdapter
from .workers.base import HeadlessCoder


WORKER_ADAPTERS: dict[str, type[HeadlessCoder]] = {
    "codex": CodexAdapter,
    "grok": GrokAdapter,
    "claude": ClaudeAdapter,
}


class CrewAIHeadlessFlow(Flow[FlowState]):
    """
    Reusable, config-driven, multi-agent Flow that uses agent-skills as
    operating procedures and delegates actual code work to pluggable
    headless coders (Codex, Grok, or Claude).
    """

    def __init__(self, config: FlowConfig | None = None):
        super().__init__()
        self.config = config or get_default_config()
        self.loader = get_default_loader()
        self._workers: dict[str, HeadlessCoderTool] = {}
        self._setup_workers()

    def _setup_workers(self) -> None:
        """Pre-instantiate the right worker + skill for each stage."""
        for stage in self.config.stages:
            stage_cfg = self.config.get_stage(stage)
            skill_name = stage_cfg.skill

            adapter_cls = WORKER_ADAPTERS.get(stage_cfg.worker)
            if adapter_cls is None:
                supported = ", ".join(sorted(WORKER_ADAPTERS))
                raise ValueError(
                    f"Unsupported worker '{stage_cfg.worker}' configured for stage "
                    f"'{stage}'. Supported workers: {supported}"
                )
            base_worker = adapter_cls()

            tool = HeadlessCoderTool(
                worker=base_worker,
                skill_name=skill_name,
            )
            self._workers[stage] = tool

    def _get_worker(self, stage: str) -> HeadlessCoderTool:
        if stage not in self._workers:
            raise KeyError(f"No worker configured for stage '{stage}'")
        return self._workers[stage]

    def _normalize_review_payload(
        self, data: dict
    ) -> tuple[Literal["pass", "revise"], list[str]]:
        raw_status = data.get("status")
        status: Literal["pass", "revise"] = "pass" if raw_status == "pass" else "revise"

        raw_issues = data.get("issues", [])
        if isinstance(raw_issues, list):
            issues = [str(issue) for issue in raw_issues]
        elif raw_issues:
            issues = [str(raw_issues)]
        else:
            issues = []

        if raw_status not in {"pass", "revise"}:
            issues = [f"Review returned invalid status: {raw_status}"]
        if status == "pass" and issues:
            status = "revise"

        return status, issues

    def _extract_review_payload(self, raw: str) -> dict | None:
        if not raw:
            return None

        try:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start == -1 or end <= start:
                return None
            data = json.loads(raw[start:end])
        except (TypeError, ValueError):
            return None

        if not isinstance(data, dict):
            return None
        if "status" in data:
            return data

        for key in ("result", "text", "content", "message", "summary"):
            value = data.get(key)
            if isinstance(value, dict) and "status" in value:
                return value
            if isinstance(value, str):
                nested = self._extract_review_payload(value)
                if nested is not None:
                    return nested

        return None

    def _is_human_feedback_enabled(self) -> bool:
        return bool(self.config.human_feedback.get("enabled", False))

    def _is_terminal_status(self) -> bool:
        return self.state.status in {"completed", "aborted_by_human", "failed"}

    def _mark_running(self) -> None:
        if not self._is_terminal_status():
            self.state.status = "running"

    def _terminal_result(self) -> str:
        if self.state.status == "aborted_by_human":
            return "aborted-by-human"
        return f"terminal-status-{self.state.status}"

    def _human_feedback_prompt(self, stage: str, message: str) -> str:
        stage_cfg = self.config.get_stage(stage)
        can_mutate = "yes" if stage in {"do_work", "finalize"} else "no"
        return f"""
[Human Feedback]
{message}

Stage: {stage}
Can mutate files: {can_mutate}
Worker: {stage_cfg.worker}
Skill: {stage_cfg.skill}
Target repo: {self.state.target_repo or "(not set)"}
Default: no
""".strip()

    def _maybe_ask_human(self, stage: str, message: str) -> bool:
        """
        If human feedback is enabled for this point, ask the user.
        Returns True if we should proceed.
        """
        hf = self.config.human_feedback

        if not hf.get("enabled", False):
            return True

        # Check specific gate
        gate = f"before_{stage}"
        if not hf.get(gate, True):
            return True

        print(f"\n{self._human_feedback_prompt(stage, message)}")
        try:
            answer = input("Proceed? [y/N]: ").strip().lower()
            return answer in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            print("\n[Human Feedback] No input received. Aborting this step.")
            return False

    def _mark_human_abort(self, stage: str) -> None:
        self.state.status = "aborted_by_human"
        self.state.aborted_stage = stage
        self.state.last_stage = stage
        self.state.errors.append(f"Aborted by human before {stage}")

    # ------------------------------------------------------------------
    # Planning stage (orchestration LLM + skills)
    # ------------------------------------------------------------------
    @start()
    def plan(self) -> str:
        self._mark_running()
        print_stage_mapping()  # Visibility into current wiring

        stage_cfg = self.config.get_stage("plan")
        skill_name = stage_cfg.skill

        # Build the guidance from the skill
        guidance = self.loader.get_core_guidance(skill_name)

        # Simple but effective planning agent on local Ollama
        llm = LLM(
            model="ollama/llama3.2",
            base_url="http://localhost:11434",
            temperature=0.3,
        )

        planner = Agent(
            role="Senior Software Architect & Planner",
            goal="Create a clear, actionable spec and task breakdown",
            backstory="You are an expert at turning vague requests into well-structured, testable plans using proven methodologies.",
            llm=llm,
            verbose=False,
        )

        planning_task = Task(
            description=f"""
You must follow this exact operating procedure:

{guidance}

---

User request:
{self.state.request}

Target repository: {self.state.target_repo}

Produce:
1. A concise but complete **spec** (objective, success criteria, boundaries).
2. A **task list** broken into small vertical slices with acceptance criteria.

Output format:
<spec>
[your spec here]
</spec>

<tasks>
[numbered task list]
</tasks>
""".strip(),
            agent=planner,
            expected_output="A spec and a numbered task list following the procedure above.",
        )

        result = planning_task.execute_sync()
        output = result.raw or str(result)

        # Very lightweight parsing for the spike/demo
        self.state.spec = output
        self.state.last_stage = "plan"

        # For a real implementation we would parse tasks into TaskItem objects here.
        # For M5 we keep it simple and pass the raw plan forward.
        print("\n[Flow] Planning complete. Spec length:", len(output))
        return output

    # ------------------------------------------------------------------
    # Core work stage - delegates to the configured headless coder (edit mode)
    # ------------------------------------------------------------------
    @listen("plan")
    def do_work(self, plan_output: str) -> str:
        if self._is_terminal_status():
            print(
                f"[Flow] Skipping do_work because flow is terminal: {self.state.status}"
            )
            return self._terminal_result()
        self._mark_running()
        if not self._maybe_ask_human(
            "do_work",
            "About to run the expensive edit stage (do_work). This will let the headless coder modify files.",
        ):
            print("[Flow] Human aborted before do_work.")
            self._mark_human_abort("do_work")
            return "aborted-by-human"

        worker_tool = self._get_worker("do_work")
        stage_cfg = self.config.get_stage("do_work")

        print(f"\n[Flow] do_work using {stage_cfg.worker} (skill: {stage_cfg.skill})")

        prompt = f"""Follow the assigned operating procedure for this implementation stage.

Plan / spec context:
{plan_output[:3000]}

Original user request:
{self.state.request}

Target repo: {self.state.target_repo}

Current revision count: {self.state.revisions}

Execute the work. After you are done, summarize what changed and whether tests now pass.
"""

        result = worker_tool.run(
            task=prompt,
            cwd=self.state.target_repo,
            mode="edit",
            timeout=stage_cfg.timeout,
            model=stage_cfg.model,
        )

        if result.changed_files:
            self.state.changed_files.extend(result.changed_files)

        self.state.last_stage = "do_work"
        return result.summary or result.raw_output

    # ------------------------------------------------------------------
    # Review router - uses configured worker in INSPECT (read-only) mode
    # ------------------------------------------------------------------
    @router("do_work")
    def review(self, work_summary: str) -> Literal["pass", "revise", "aborted"]:
        if self._is_terminal_status():
            print(
                f"[Flow] Skipping review because flow is terminal: {self.state.status}"
            )
            return "aborted"
        self._mark_running()
        worker_tool = self._get_worker("review")
        stage_cfg = self.config.get_stage("review")

        print(
            f"\n[Flow] review using {stage_cfg.worker} in INSPECT mode (skill: {stage_cfg.skill})"
        )

        prompt = f"""You are performing a rigorous code review following the assigned procedure.

Work that was just performed:
{work_summary}

Original request:
{self.state.request}

Changed files so far: {self.state.changed_files}

Respond with a single JSON object ONLY (no other text):

{{
  "status": "pass" or "revise",
  "issues": [
    "specific issue 1",
    "specific issue 2"
  ],
  "summary": "one sentence overall assessment"
}}

If everything looks good according to the review procedure, use "pass".
Otherwise use "revise" and list the concrete issues that must be addressed.
"""

        self.state.last_stage = "review"

        crew_cfg = stage_cfg.extra.get("crew", {}) or {}
        status: str = "revise"
        issues: list[str] = []
        if crew_cfg.get("enabled", False):
            try:
                decision = run_review_crew(
                    review_context=prompt,
                    worker_tool=worker_tool,
                    cwd=self.state.target_repo,
                    timeout=stage_cfg.timeout,
                    model=stage_cfg.model,
                    crew_config=crew_cfg,
                )
            except Exception as exc:
                decision = ReviewCrewDecision(
                    status="revise",
                    issues=[f"Review Crew failed: {exc}"],
                    summary="Review Crew failed before producing a decision.",
                )

            status = decision.status
            issues = decision.issues
        else:
            result = worker_tool.run(
                task=prompt,
                cwd=self.state.target_repo,
                mode="inspect",  # Critical: read-only guarantee
                timeout=stage_cfg.timeout,
                model=stage_cfg.model,
            )

            for raw in (result.raw_output, result.summary):
                data = self._extract_review_payload(raw or "")
                if data is not None:
                    status, issues = self._normalize_review_payload(data)
                    break
            else:
                status = "revise"
                issues = ["Review output could not be parsed as structured JSON"]

        self.state.review_status = status  # type: ignore
        self.state.issues = issues

        print(f"[Flow] Review decision: {status} | Issues: {len(issues)}")
        return status  # type: ignore

    # ------------------------------------------------------------------
    # Bounded revise loop
    # ------------------------------------------------------------------
    @listen("revise")
    def revise(self, decision: str) -> str:
        if self._is_terminal_status():
            print(
                f"[Flow] Skipping revise because flow is terminal: {self.state.status}"
            )
            return self._terminal_result()
        self._mark_running()
        self.state.increment_revision()
        print(
            f"\n[Flow] Revising (revision {self.state.revisions}/{self.state.max_revisions})"
        )

        if self.state.revisions >= self.state.max_revisions:
            print("[Flow] Max revisions reached. Forcing pass to avoid infinite loop.")
            self.state.review_status = "pass"
            return "max-revisions-reached"

        # Loop back to do_work with the issues as additional context
        issues_text = "\n".join(f"- {i}" for i in self.state.issues)
        return f"Previous review found the following issues that must be fixed:\n{issues_text}"

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------
    @listen("pass")
    def finalize(self, _decision: str) -> str:
        if self._is_terminal_status():
            print(
                f"[Flow] Skipping finalize because flow is terminal: {self.state.status}"
            )
            return self._terminal_result()
        self._mark_running()
        if not self._maybe_ask_human(
            "finalize",
            "About to finalize and write documentation/ADR. This is the last step.",
        ):
            print("[Flow] Human aborted before finalize.")
            self._mark_human_abort("finalize")
            return "aborted-by-human"

        worker_tool = self._get_worker("finalize")
        stage_cfg = self.config.get_stage("finalize")

        print(f"\n[Flow] finalize using {stage_cfg.worker} (skill: {stage_cfg.skill})")

        prompt = f"""Create final documentation / ADR for the completed work.

Original request: {self.state.request}
Spec: {self.state.spec[:1500] if self.state.spec else "N/A"}
Revisions used: {self.state.revisions}
Files changed: {self.state.changed_files}

Write a concise ADR or completion report suitable for the repository.
"""

        result = worker_tool.run(
            task=prompt,
            cwd=self.state.target_repo,
            mode="edit",
            timeout=stage_cfg.timeout,
            model=stage_cfg.model,
        )

        self.state.final_artifact = result.summary or result.raw_output
        self.state.last_stage = "finalize"
        self.state.status = "completed"

        print("[Flow] Flow completed successfully.")
        return self.state.final_artifact or "Flow completed"

    @listen("aborted")
    def aborted(self, _decision: str) -> str:
        print(f"[Flow] Flow stopped at terminal status: {self.state.status}")
        return self._terminal_result()


# Convenience runner for demos / CLI
def run_headless_flow(
    request: str,
    target_repo: str,
    max_revisions: int = 2,
    config: FlowConfig | None = None,
) -> FlowState:
    """
    High-level entry point for running the full flow.
    """
    state = FlowState(
        request=request,
        target_repo=target_repo,
        max_revisions=max_revisions,
    )

    flow = CrewAIHeadlessFlow(config=config)
    flow.kickoff(inputs=state.model_dump())

    return flow.state
