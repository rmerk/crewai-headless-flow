"""
The main reusable CrewAI Flow for multi-agent headless coding with pluggable workers.

Canonical topology lives in ``config/flow.yaml`` (``schema: crewai.flow/v1``) and
is bound at construction by ``build_headless_flow`` (ADR-0012). Stage bodies stay
in Python behind ``call: code`` refs into ``stages.*``.

Graph:
- plan (start) → do_work → review (router: pass|revise|aborted)
- revise → process_revision → do_work
- pass → finalize; aborted/failed → terminal handlers
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal, cast

from crewai.flow.flow import Flow

from .config import (
    FlowConfig,
    StageConfig,
    classify_stage_extra,
    get_default_config,
)
from .flow_topology import load_flow_definition
from .human_feedback_actions import (
    default_human_feedback_gate,
    human_feedback_action_prompt_token,
    is_after_review_gate,
    is_default_human_feedback_gate,
    parse_human_feedback_stage_action,
    stage_mutates_files,
    supported_human_feedback_actions,
    StageName,
)
from .hitl_policy import (
    GateContext,
    describe_trigger_reason,
    should_prompt,
)
from .escalation import (
    EscalationHandler,
    EscalationRequest,
    get_handler as get_escalation_handler,
)
from .plan_contract import (
    PlanOutput,
    render_plan_markdown,
    state_items_to_plan_output,
)
from .review_contract import (
    ReviewDecision,
    ReviewTaskHint,
)
from .review_crew import ReviewCrewDecision
from .reporting import render_execution_report
from .run_store import RunStore
from .skills.loader import get_default_loader
from .state import (
    AbortedCheckpoint,
    CrewRoundEntry,
    FlowHistoryEntry,
    FlowState,
    HumanFeedbackEntry,
    StageRuntimeSnapshot,
    TaskItem,
    TriggerReason,
)
from .tools.coder_tool import HeadlessCoderTool
from .verification import VerificationReport, VerifyRunner
from .workers import WORKER_SPECS
from .workers.base import CoderResult, HeadlessCoder
from .stages import plan as plan_stage
from .stages import revision as revision_stage
from .stages import terminal as terminal_stage
from .stages import review as review_stage
from .stages import finalize as finalize_stage
from .stages import do_work as do_work_stage
from .stages.do_work import ParallelTaskOutcome, PlannedBatchSelection


logger = logging.getLogger(__name__)


# The Flow's only knowledge of concrete workers (AGENTS.md invariant home),
# derived from the single WORKER_SPECS registration table.
WORKER_ADAPTERS: dict[str, type[HeadlessCoder]] = {
    name: spec.adapter_cls for name, spec in WORKER_SPECS.items()
}


@dataclass
class HumanFeedbackDecision:
    proceed: bool
    action: str
    response: str
    instructions: str | None = None
    task_ids: list[int] | None = None


class CrewAIHeadlessFlow(Flow[FlowState]):
    """
    Reusable, config-driven, multi-agent Flow that uses agent-skills as
    operating procedures and delegates actual code work to pluggable
    headless coders (Codex, Grok, or Claude).
    """

    def __init__(
        self,
        config: FlowConfig | None = None,
        run_store: RunStore | None = None,
    ):
        super().__init__()
        self.config = config or get_default_config()
        self.loader = get_default_loader()
        self._run_store = run_store
        # Test/injection seam; when None the handler is resolved from config
        # at ask time (so a run_store attached after construction — the
        # resume path — still reaches the file channel).
        self._escalation: EscalationHandler | None = None
        # Test/injection seam for the Flow-owned verification subprocess
        # boundary (never routed through a worker adapter).
        self._verification_runner: VerifyRunner = subprocess.run
        # Clock seam so event-log timestamps are deterministic in tests
        # (same pattern as run_store.generate_run_id(now=...)).
        self._now_fn: Callable[[], datetime] = datetime.now
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
            base_worker = self._instantiate_worker(adapter_cls, stage_cfg.worker)

            fallback_worker = self._resolve_fallback_worker(stage, stage_cfg)
            retry_cfg = stage_cfg.extra.get("retry") or {}

            tool = HeadlessCoderTool(
                worker=base_worker,
                skill_name=skill_name,
                fallback_worker=fallback_worker,
                max_attempts=int(retry_cfg.get("max_attempts", 1)),
                backoff_seconds=float(retry_cfg.get("backoff_seconds", 0.0)),
            )
            self._workers[stage] = tool

    def _resolve_fallback_worker(
        self, stage: str, stage_cfg: StageConfig
    ) -> HeadlessCoder | None:
        fallback_name = stage_cfg.extra.get("fallback_worker")
        if not fallback_name:
            return None
        if fallback_name == stage_cfg.worker:
            raise ValueError(
                f"fallback_worker for stage '{stage}' must differ from the "
                f"stage worker '{stage_cfg.worker}'"
            )
        fallback_cls = WORKER_ADAPTERS.get(fallback_name)
        if fallback_cls is None:
            supported = ", ".join(sorted(WORKER_ADAPTERS))
            raise ValueError(
                f"Unsupported fallback worker '{fallback_name}' configured for "
                f"stage '{stage}'. Supported workers: {supported}"
            )
        return self._instantiate_worker(fallback_cls, fallback_name)

    def _instantiate_worker(
        self, adapter_cls: type[HeadlessCoder], worker_name: str
    ) -> HeadlessCoder:
        """Build an adapter, honoring configured settings (binary, effort).

        Every adapter accepts ``binary=``; the zero-arg call keeps their
        defaults when no override is configured.
        """
        import inspect

        settings = getattr(self.config, "worker_settings", {}) or {}
        worker_cfg = settings.get(worker_name) or {}
        kwargs = {}
        if "binary" in worker_cfg and worker_cfg["binary"]:
            kwargs["binary"] = worker_cfg["binary"]
        if "effort" in worker_cfg and worker_cfg["effort"]:
            sig = inspect.signature(adapter_cls.__init__)
            if "effort" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                kwargs["effort"] = worker_cfg["effort"]

        return adapter_cls(**kwargs)  # type: ignore[call-arg]

    def _get_worker(self, stage: str) -> HeadlessCoderTool:
        if stage not in self._workers:
            raise KeyError(f"No worker configured for stage '{stage}'")
        return self._workers[stage]

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

    def _revision_replan_requested(self) -> bool:
        return bool(self.state.pending_revision_replan)

    def _set_pending_revision_replan(self, reason: str | None) -> str:
        resolved_reason = (
            reason or "Human requested replanning before the next revise loop."
        )
        self.state.pending_revision_replan = True
        self.state.pending_revision_replan_reason = resolved_reason
        return resolved_reason

    def _clear_pending_revision_replan(self) -> None:
        self.state.pending_revision_replan = False
        self.state.pending_revision_replan_reason = None

    def _can_replan_before_do_work(self) -> bool:
        return self.state.revisions == 0 and not self.state.task_executions

    def _expand_required_task_ids(self, seed_ids: set[int]) -> set[int]:
        task_map = {task.id: task for task in self.state.tasks}
        expanded: set[int] = set()
        stack = list(seed_ids)

        while stack:
            task_id = stack.pop()
            if task_id in expanded:
                continue
            task = task_map.get(task_id)
            if task is None:
                continue
            expanded.add(task_id)
            for dependency in task.dependencies:
                if dependency in task_map and dependency not in expanded:
                    stack.append(dependency)

        return expanded

    def _is_human_feedback_action_available(
        self,
        stage: StageName,
        action: str,
        gate: str | None = None,
    ) -> bool:
        if stage == "do_work" and action == "replan":
            return self._can_replan_before_do_work()
        if stage == "do_work" and action == "target-tasks":
            return bool(self.state.tasks)
        if stage == "review" and action == "replan":
            return bool(self.state.tasks)
        if stage == "review" and action == "rerun-review":
            return is_after_review_gate(stage, gate)
        if stage == "review" and action == "target-tasks":
            return bool(self.state.tasks)
        if stage == "finalize" and action == "rerun-review":
            return bool(self.state.latest_work_summary)
        if stage == "finalize" and action in {"replan", "target-tasks"}:
            return bool(self.state.tasks)
        return True

    def _enabled_human_feedback_actions(
        self,
        stage: StageName,
        gate: str | None = None,
    ) -> list[str]:
        supported = [
            action
            for action in supported_human_feedback_actions(gate or stage)
            if self._is_human_feedback_action_available(stage, action, gate)
        ]
        if not supported:
            return []

        raw_allowlist = self.config.human_feedback.get("action_allowlist", {}) or {}
        if isinstance(raw_allowlist, dict):
            if gate and gate in raw_allowlist:
                allowlist = raw_allowlist.get(gate, [])
                if isinstance(allowlist, list):
                    return [action for action in allowlist if action in supported]
                return []
            if stage in raw_allowlist:
                allowlist = raw_allowlist.get(stage, [])
                if isinstance(allowlist, list):
                    return [action for action in allowlist if action in supported]
                return []

        if bool(self.config.human_feedback.get("advanced_actions", False)):
            return supported
        return []

    def _human_feedback_options(
        self, stage: StageName, gate: str | None = None
    ) -> list[str]:
        options = ["approve=y/yes", "abort=n/no/empty"]
        options.extend(
            human_feedback_action_prompt_token(stage, action)
            for action in self._enabled_human_feedback_actions(stage, gate)
        )
        return options

    def _should_capture_human_instructions(
        self,
        *,
        action: str,
        hf: dict[str, object],
    ) -> bool:
        return (
            action == "approve" and bool(hf.get("capture_instructions", False))
        ) or (action in {"force-revise", "replan", "rerun-review", "target-tasks"})

    def _default_human_instruction(
        self,
        *,
        stage: StageName,
        gate: str,
        action: str,
    ) -> str | None:
        if action == "force-revise":
            if stage == "review" and gate == "after_review":
                return "Human requested revision after automated review."
            if stage == "finalize":
                return "Human requested revision before finalize."
            if stage == "review":
                return "Human requested revision before inspect-mode review."
            return "Human requested revision."
        if action == "replan":
            if stage == "do_work":
                return "Human requested replanning before the edit stage."
            if stage == "finalize":
                return "Human requested replanning before finalize."
            if stage == "review" and gate == "after_review":
                return "Human requested replanning after automated review."
            if stage == "review":
                return "Human requested replanning before automated review."
            return "Human requested replanning."
        if action == "rerun-review":
            if stage == "finalize":
                return "Human requested automated review rerun before finalize."
            return "Human requested automated review rerun."
        if action == "target-tasks":
            if stage == "do_work":
                return "Human selected tasks for focused execution before do_work."
            if stage == "review" and gate == "after_review":
                return (
                    "Human selected tasks for targeted revision after automated review."
                )
            if stage == "finalize":
                return "Human selected tasks for targeted revision before finalize."
            if stage == "review":
                return "Human selected tasks for targeted revision before automated review."
            return "Human selected tasks for targeted revision."
        return None

    def _prompt_for_human_instructions(
        self,
        *,
        stage: StageName,
        gate: str,
        action: str,
    ) -> str | None:
        if action == "force-revise":
            prompt = "Revision issues / instructions: "
        elif action == "replan":
            prompt = "Replan instructions: "
        elif action == "rerun-review":
            prompt = "Review rerun instructions: "
        elif action == "target-tasks":
            prompt = (
                "Execution note (optional): "
                if stage == "do_work"
                else "Revision note (optional): "
            )
        else:
            prompt = "Additional instructions (optional): "
        try:
            raw_instructions = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            if action == "force-revise":
                print(
                    "\n[Human Feedback] No revision notes received. Continuing with a generic forced-revise reason."
                )
            elif action == "replan":
                print(
                    "\n[Human Feedback] No replan notes received. Continuing with a generic replanning reason."
                )
            elif action == "rerun-review":
                print(
                    "\n[Human Feedback] No rerun notes received. Continuing with a generic review-rerun reason."
                )
            elif action == "target-tasks":
                if stage == "do_work":
                    print(
                        "\n[Human Feedback] No execution note received. Continuing with a generic targeted-execution reason."
                    )
                else:
                    print(
                        "\n[Human Feedback] No revision note received. Continuing with a generic targeted-revision reason."
                    )
            else:
                print(
                    "\n[Human Feedback] No instruction input received. Continuing without extra instructions."
                )
            raw_instructions = ""

        return raw_instructions or self._default_human_instruction(
            stage=stage,
            gate=gate,
            action=action,
        )

    def _prompt_for_target_task_ids(self) -> list[int] | None:
        available_ids = [task.id for task in self.state.tasks]
        available_text = ", ".join(str(task_id) for task_id in available_ids) or "none"
        hinted_ids = self._hinted_task_ids()
        hinted_text = (
            ", or `hinted` (" + ", ".join(str(task_id) for task_id in hinted_ids) + ")"
            if hinted_ids
            else ""
        )
        print("[Human Feedback] Available tasks for targeting:")
        print(self._current_task_graph_summary())
        if hinted_ids:
            print("[Human Feedback] Suggested review targets:")
            print(self._review_target_summary())
        try:
            raw_task_ids = input(
                "Target task ids, ranges, or file selectors "
                "(use `file:<path>` for exact file matches; "
                f"available ids: {available_text}; or `all`{hinted_text}): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[Human Feedback] No task targets received. Aborting this step.")
            return None

        if not raw_task_ids:
            print("\n[Human Feedback] No task targets received. Aborting this step.")
            return None

        known_ids = set(available_ids)
        parsed_ids: list[int] = []
        invalid_file_selectors: list[str] = []
        hinted_requested_without_matches = False
        try:
            for part in raw_task_ids.split(","):
                token = part.strip()
                if not token:
                    continue
                lowered = token.lower()
                if lowered == "all":
                    return list(available_ids)
                if lowered == "hinted":
                    if hinted_ids:
                        parsed_ids.extend(hinted_ids)
                    else:
                        hinted_requested_without_matches = True
                    continue
                if lowered.startswith("file:"):
                    matched_ids = self._task_ids_for_file_selector(token[5:])
                    if matched_ids:
                        parsed_ids.extend(matched_ids)
                    else:
                        invalid_file_selectors.append(token[5:].strip())
                    continue
                range_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
                if range_match:
                    start = int(range_match.group(1))
                    end = int(range_match.group(2))
                    step = 1 if end >= start else -1
                    parsed_ids.extend(range(start, end + step, step))
                    continue
                parsed_ids.append(int(token))
        except ValueError:
            print(
                "\n[Human Feedback] Invalid task target input. Expected comma-separated integers, ranges like `2-4`, `all`, `hinted`, or file selectors like `file:docs/readme.md`."
            )
            return None

        if hinted_requested_without_matches:
            print(
                "\n[Human Feedback] No suggested review targets are available for `hinted`."
            )
            return None

        if invalid_file_selectors:
            invalid_text = ", ".join(invalid_file_selectors)
            print(
                "\n[Human Feedback] Invalid file target selectors: "
                f"{invalid_text}. Use exact task file paths from the catalog."
            )
            return None

        if not parsed_ids:
            print("\n[Human Feedback] No task targets received. Aborting this step.")
            return None

        invalid_ids = [task_id for task_id in parsed_ids if task_id not in known_ids]
        if invalid_ids:
            invalid_text = ", ".join(str(task_id) for task_id in invalid_ids)
            print(
                "\n[Human Feedback] Invalid task target ids: "
                f"{invalid_text}. Available task ids: {available_text}."
            )
            return None

        unique_ids: list[int] = []
        seen_ids: set[int] = set()
        for task_id in parsed_ids:
            if task_id not in seen_ids:
                unique_ids.append(task_id)
                seen_ids.add(task_id)
        return unique_ids

    def _parse_human_feedback_action(
        self,
        stage: StageName,
        answer: str,
        gate: str | None = None,
    ) -> tuple[str, bool]:
        normalized = answer.strip().lower()
        if normalized in {"y", "yes"}:
            return "approve", True
        if normalized in {"", "n", "no"}:
            return "abort", False
        action = parse_human_feedback_stage_action(
            stage,
            normalized,
            self._enabled_human_feedback_actions(stage, gate),
        )
        if action is not None:
            return action, False
        return "abort", False

    def _human_feedback_prompt(self, stage: StageName, message: str, gate: str) -> str:
        stage_cfg = self.config.get_stage(stage)
        can_mutate = "yes" if stage_mutates_files(stage) else "no"
        capture_instructions = self.config.human_feedback.get(
            "capture_instructions", False
        )
        return f"""
[Human Feedback]
{message}

Stage: {stage}
Gate: {gate}
Can mutate files: {can_mutate}
Worker: {stage_cfg.worker}
Skill: {stage_cfg.skill}
Target repo: {self.state.target_repo or "(not set)"}
Default: no
Options: {" | ".join(self._human_feedback_options(stage, gate))}
Optional instructions after approval: {"enabled" if capture_instructions else "disabled"}
""".strip()

    def _record_human_feedback(
        self,
        *,
        stage: StageName,
        action: str,
        approved: bool,
        response: str,
        instructions: str | None,
        task_ids: list[int] | None,
        message: str,
        gate: str,
        trigger_reason: TriggerReason | None = None,
    ) -> None:
        stage_cfg = self.config.get_stage(stage)
        self.state.human_feedback_log.append(
            HumanFeedbackEntry(
                stage=stage,
                gate=gate,
                approved=approved,
                action=action,
                response=response,
                instructions=instructions,
                task_ids=list(task_ids or []),
                revision=self.state.revisions,
                worker=stage_cfg.worker,
                skill=stage_cfg.skill,
                can_mutate=stage_mutates_files(stage),
                message=message,
                trigger_reason=trigger_reason,
            )
        )
        self._log_event(
            "human_feedback",
            stage=stage,
            gate=gate,
            action=action,
            approved=approved,
        )
        self._refresh_debug_report()

    def _normalized_human_feedback_entries(self) -> list[HumanFeedbackEntry]:
        entries: list[HumanFeedbackEntry] = []
        for entry in self.state.human_feedback_log:
            if isinstance(entry, HumanFeedbackEntry):
                entries.append(entry)
            elif isinstance(entry, dict):
                entries.append(HumanFeedbackEntry.model_validate(entry))
        return entries

    def _latest_human_feedback_entry(
        self,
        *,
        stage: str | None = None,
        gate: str | None = None,
        approved: bool | None = None,
    ) -> HumanFeedbackEntry | None:
        for entry in reversed(self._normalized_human_feedback_entries()):
            if stage is not None and entry.stage != stage:
                continue
            if gate is not None and entry.gate != gate:
                continue
            if approved is not None and entry.approved is not approved:
                continue
            return entry
        return None

    def _build_gate_context(self, gate: str) -> GateContext:
        """Build the ``GateContext`` a gate's triggers need from live state.

        Only ``before_do_work`` needs task state in Phase 0: it fires once per
        ``do_work`` invocation *before* ``execution_target_task_ids`` is resolved,
        so the trigger scans the current task set as it stands at gate time.
        """

        if gate != "before_do_work":
            return GateContext()
        tasks = tuple(
            task if isinstance(task, TaskItem) else TaskItem.model_validate(task)
            for task in self.state.tasks
        )
        return GateContext(tasks=tasks)

    def _ask_escalation(self, request: EscalationRequest) -> str | None:
        handler = self._escalation or get_escalation_handler(
            self.config.human_feedback,
            run_dir=self._run_store.run_dir if self._run_store else None,
        )
        return handler.ask(request)

    def _maybe_ask_human(
        self,
        stage: StageName,
        message: str,
        *,
        gate: str | None = None,
    ) -> HumanFeedbackDecision:
        """
        If human feedback is enabled for this point, ask the user.
        Returns a decision that may also include operator instructions.
        """
        hf = self.config.human_feedback

        if not hf.get("enabled", False):
            return HumanFeedbackDecision(
                proceed=True,
                action="auto-disabled",
                response="auto-disabled",
            )

        # Decide whether this gate should prompt: in static mode the gate's
        # boolean decides; in conditional mode a deterministic, state-derived
        # trigger does (hitl_policy owns that logic and the reason).
        gate = gate or default_human_feedback_gate(stage)
        gate_decision = should_prompt(
            gate, hf, self.state, self._build_gate_context(gate)
        )
        if not gate_decision.should_prompt:
            return HumanFeedbackDecision(
                proceed=True,
                action="auto-gate-disabled",
                response="auto-gate-disabled",
            )

        trigger_reason = gate_decision.trigger_reason
        prompt_message = message
        if trigger_reason is not None:
            prompt_message = (
                f"{message}\n\nTrigger: {describe_trigger_reason(trigger_reason)}"
            )

        rendered_prompt = self._human_feedback_prompt(stage, prompt_message, gate)
        print(f"\n{rendered_prompt}")
        raw_answer = self._ask_escalation(
            EscalationRequest(
                stage=stage,
                gate=gate,
                prompt=rendered_prompt,
                run_id=self.state.run_id,
                run_dir=self.state.run_dir,
                target_repo=self.state.target_repo,
                revisions=self.state.revisions,
            )
        )
        if raw_answer is None:
            print("\n[Human Feedback] No input received. Aborting this step.")
            self._record_human_feedback(
                stage=stage,
                gate=gate,
                action="abort",
                approved=False,
                response="no-input",
                instructions=None,
                task_ids=None,
                message=prompt_message,
                trigger_reason=trigger_reason,
            )
            return HumanFeedbackDecision(
                proceed=False,
                action="abort",
                response="no-input",
            )

        answer = raw_answer.strip().lower()
        action, proceed = self._parse_human_feedback_action(stage, answer, gate)
        approved = action != "abort"
        instructions: str | None = None
        task_ids: list[int] | None = None

        if action == "target-tasks":
            task_ids = self._prompt_for_target_task_ids()
            if not task_ids:
                action = "abort"
                proceed = False
                approved = False

        if self._should_capture_human_instructions(action=action, hf=hf):
            instructions = self._prompt_for_human_instructions(
                stage=stage,
                gate=gate,
                action=action,
            )

        self._record_human_feedback(
            stage=stage,
            gate=gate,
            action=action,
            approved=approved,
            response=answer or "empty",
            instructions=instructions,
            task_ids=task_ids,
            message=prompt_message,
            trigger_reason=trigger_reason,
        )
        return HumanFeedbackDecision(
            proceed=proceed,
            action=action,
            response=answer or "empty",
            instructions=instructions,
            task_ids=task_ids,
        )

    def _mark_human_abort(
        self,
        stage: StageName,
        stage_input: str | None = None,
        *,
        gate: str | None = None,
        message: str | None = None,
        before_review_instructions: str | None = None,
    ) -> None:
        self.state.status = "aborted_by_human"
        gate = gate or default_human_feedback_gate(stage)
        self.state.set_aborted_checkpoint(
            stage=stage,
            gate=gate,
            message=message,
            before_review_instructions=before_review_instructions,
            stage_input=stage_input,
        )
        self.state.last_stage = stage
        if is_default_human_feedback_gate(stage, gate):
            self.state.errors.append(f"Aborted by human before {stage}")
        else:
            self.state.errors.append(f"Aborted by human at {gate}")
        self._log_event("human_abort", stage=stage, gate=gate)
        self._refresh_debug_report()

    def _after_review_message(self, decision: ReviewCrewDecision) -> str:
        return review_stage.format_after_review_message(self, decision)

    def _build_review_prompt(
        self,
        *,
        work_summary: str,
        human_guidance: str | None = None,
        review_rerun_guidance: str | None = None,
        verification_evidence: str | None = None,
    ) -> str:
        return review_stage.build_review_prompt(
            self,
            work_summary=work_summary,
            human_guidance=human_guidance,
            review_rerun_guidance=review_rerun_guidance,
            verification_evidence=verification_evidence,
        )

    def _run_verification_round(self) -> VerificationReport | None:
        return review_stage.run_verification_round(self)

    def _verification_revise_decision(
        self, report: VerificationReport
    ) -> ReviewDecision:
        return review_stage.verification_revise_decision(self, report)

    def _render_verification_evidence(
        self, report: VerificationReport | None
    ) -> str | None:
        return review_stage.render_verification_evidence(self, report)

    def _record_review_decision_state(self, decision: ReviewDecision) -> None:
        return review_stage.record_review_decision_state(self, decision)

    def _fail_closed_for_incomplete_structured_tasks(
        self, decision: ReviewDecision
    ) -> ReviewDecision:
        return review_stage.fail_closed_for_incomplete_structured_tasks(self, decision)

    def _run_automated_review_once(
        self,
        *,
        work_summary: str,
        human_guidance: str | None = None,
        review_rerun_guidance: str | None = None,
        verification_evidence: str | None = None,
    ) -> ReviewDecision:
        return review_stage.run_automated_review_once(
            self,
            work_summary=work_summary,
            human_guidance=human_guidance,
            review_rerun_guidance=review_rerun_guidance,
            verification_evidence=verification_evidence,
        )

    def _run_verified_review_once(
        self,
        *,
        work_summary: str,
        human_guidance: str | None = None,
        review_rerun_guidance: str | None = None,
    ) -> ReviewDecision:
        return review_stage.run_verified_review_once(
            self,
            work_summary=work_summary,
            human_guidance=human_guidance,
            review_rerun_guidance=review_rerun_guidance,
        )

    def _handle_after_review_checkpoint(
        self,
        *,
        work_summary: str,
        message: str,
        automated_status: Literal["pass", "revise"],
        human_guidance: str | None = None,
    ) -> tuple[Literal["pass", "revise", "aborted", "rerun-review"], str | None]:
        return review_stage.handle_after_review_checkpoint(
            self,
            work_summary=work_summary,
            message=message,
            automated_status=automated_status,
            human_guidance=human_guidance,
        )

    def _resume_after_review_checkpoint(
        self,
        work_summary: str,
        *,
        saved_message: str | None = None,
        saved_before_review_instructions: str | None = None,
    ) -> Literal["pass", "revise", "aborted"]:
        return review_stage.resume_after_review_checkpoint(
            self,
            work_summary,
            saved_message=saved_message,
            saved_before_review_instructions=saved_before_review_instructions,
        )

    def _plan_crew_enabled(self, stage_cfg) -> bool:
        return plan_stage.plan_crew_enabled(self, stage_cfg)

    def _current_task_graph_summary(self) -> str:
        return plan_stage.current_task_graph_summary(self)

    def _build_plan_prompt(
        self,
        human_instructions: str | None = None,
        *,
        current_plan_output: str | None = None,
        replanning_reason: str | None = None,
    ) -> str:
        return plan_stage.build_plan_prompt(
            self,
            human_instructions,
            current_plan_output=current_plan_output,
            replanning_reason=replanning_reason,
        )

    def _execute_plan_stage(
        self,
        *,
        human_instructions: str | None = None,
        current_plan_output: str | None = None,
        replanning_reason: str | None = None,
    ) -> str:
        return plan_stage.execute_plan_stage(
            self,
            human_instructions=human_instructions,
            current_plan_output=current_plan_output,
            replanning_reason=replanning_reason,
        )

    def plan(self) -> str:
        return plan_stage.execute_plan(self)

    # ------------------------------------------------------------------
    # Core work stage - delegates to the configured headless coder (edit mode)
    # ------------------------------------------------------------------
    def _parallel_do_work_enabled(self, stage_cfg) -> bool:
        return do_work_stage.parallel_do_work_enabled(self, stage_cfg)

    def _do_work_crew_enabled(self, stage_cfg) -> bool:
        return do_work_stage.do_work_crew_enabled(self, stage_cfg)

    def _record_history(
        self,
        *,
        kind: Literal[
            "batch_planning",
            "execution_targeting",
            "execution_replanning",
            "human_replanning",
            "revision_replanning",
            "task_complete",
            "task_failed",
            "review_decision",
            "revision_targeting",
        ],
        summary: str,
        task_ids: list[int] | None = None,
        files: list[str] | None = None,
        details: list[str] | None = None,
    ) -> None:
        self.state.history.append(
            FlowHistoryEntry(
                kind=kind,
                revision=self.state.revisions,
                summary=summary,
                task_ids=task_ids or [],
                files=files or [],
                details=details or [],
            )
        )
        # The single history funnel doubles as the event-log funnel: every
        # FlowHistoryEntry kind is also a JSONL event kind.
        self._log_event(
            kind,
            summary=summary,
            task_ids=task_ids or [],
            files=files or [],
            details=details or [],
        )
        self._refresh_debug_report()

    def _history_summary(self, limit: int = 8) -> str:
        if not self.state.history:
            return "- No history recorded"
        return "\n".join(
            render_execution_report(self.state, history_limit=limit).splitlines()[
                -limit:
            ]
        )

    def _normalized_history_entries(self) -> list[FlowHistoryEntry]:
        entries: list[FlowHistoryEntry] = []
        for entry in self.state.history:
            if isinstance(entry, FlowHistoryEntry):
                entries.append(entry)
            elif isinstance(entry, dict):
                entries.append(FlowHistoryEntry.model_validate(entry))
        return entries

    def _resolved_stage_snapshots(self) -> list[StageRuntimeSnapshot]:
        snapshots: list[StageRuntimeSnapshot] = []
        for stage in self.config.stages:
            cfg = self.config.get_stage(stage)
            runtime_knobs, enforced_declarations, notes = classify_stage_extra(
                stage, cfg.extra
            )
            snapshots.append(
                StageRuntimeSnapshot(
                    stage=stage,
                    skill=cfg.skill,
                    worker=cfg.worker,
                    model=cfg.model,
                    timeout=cfg.timeout,
                    extra=dict(cfg.extra),
                    runtime_knobs=runtime_knobs,
                    enforced_declarations=enforced_declarations,
                    notes=notes,
                    can_mutate=stage_mutates_files(cast(StageName, stage)),
                )
            )
        return snapshots

    def _resolved_human_feedback_config(self) -> dict[str, object]:
        return dict(self.config.human_feedback)

    def _sync_runtime_snapshot(self) -> None:
        self.state.resolved_stages = self._resolved_stage_snapshots()
        self.state.resolved_human_feedback = self._resolved_human_feedback_config()

    def _refresh_debug_report(self) -> None:
        self._sync_runtime_snapshot()
        self.state.debug_report = render_execution_report(self.state)
        self._persist_checkpoint()

    def _persist_checkpoint(self) -> None:
        # Piggybacks on _refresh_debug_report, which is already called at
        # every stage tail, review decision, task completion/failure, human
        # feedback record, and abort — so checkpoint coverage stays in sync
        # with report coverage by construction. A checkpoint write failure
        # must never kill the run.
        if self._run_store is None:
            return
        try:
            self._run_store.save_state(
                json.dumps(self.state.model_dump(), indent=2, sort_keys=True)
            )
            self._run_store.save_debug_report(self.state.debug_report or "")
        except OSError as exc:
            logger.warning(f"[RunStore] WARNING: checkpoint write failed: {exc}")

    def _log_event(self, kind: str, **fields: Any) -> None:
        """Append one structured event to runs/<run_id>/events.jsonl.

        Envelope: {ts, run_id, revision, kind, ...kind-specific fields}.
        Same durability policy as _persist_checkpoint: no run dir means no
        event log, and a write failure never kills the run.
        """
        if self._run_store is None:
            return
        event = {
            "ts": self._now_fn().isoformat(timespec="seconds"),
            "run_id": self.state.run_id,
            "revision": self.state.revisions,
            "kind": kind,
            **fields,
        }
        try:
            self._run_store.append_event(json.dumps(event, sort_keys=True))
        except OSError as exc:
            logger.warning(f"[RunStore] WARNING: event write failed: {exc}")

    def _build_task_execution_prompt(
        self,
        task: TaskItem,
        plan_output: str,
        human_instructions: str | None = None,
    ) -> str:
        return do_work_stage.build_task_execution_prompt(
            self, task, plan_output, human_instructions
        )

    def _track_changed_files(self, changed_files: list[str]) -> None:
        return do_work_stage.track_changed_files(self, changed_files)

    def _next_task_attempt(self, task_id: int) -> int:
        return do_work_stage.next_task_attempt(self, task_id)

    def _record_task_execution(
        self,
        *,
        task: TaskItem,
        stage_cfg,
        cwd: str,
        result: CoderResult,
        changed_files: list[str],
        crew_rounds: list[CrewRoundEntry],
        parallel_batch_id: str | None,
    ) -> None:
        return do_work_stage.record_task_execution(
            self,
            task=task,
            stage_cfg=stage_cfg,
            cwd=cwd,
            result=result,
            changed_files=changed_files,
            crew_rounds=crew_rounds,
            parallel_batch_id=parallel_batch_id,
        )

    def _run_task_with_change_tracking(
        self,
        *,
        worker_tool: HeadlessCoderTool,
        task: TaskItem,
        cwd: str,
        stage_cfg,
        plan_output: str,
        human_instructions: str | None = None,
        parallel_batch_id: str | None = None,
    ):
        return do_work_stage.run_task_with_change_tracking(
            self,
            worker_tool=worker_tool,
            task=task,
            cwd=cwd,
            stage_cfg=stage_cfg,
            plan_output=plan_output,
            human_instructions=human_instructions,
            parallel_batch_id=parallel_batch_id,
        )

    def _deny_patterns(self) -> list[str]:
        return do_work_stage.deny_patterns(self)

    def _denied_failure_result(
        self, denied: dict[str, str], unrestorable: list[str]
    ) -> CoderResult:
        return do_work_stage.denied_failure_result(self, denied, unrestorable)

    def _run_serial_task(
        self,
        *,
        worker_tool: HeadlessCoderTool,
        task: TaskItem,
        stage_cfg,
        plan_output: str,
        human_instructions: str | None,
    ) -> tuple[CoderResult, list[str]]:
        return do_work_stage.run_serial_task(
            self,
            worker_tool=worker_tool,
            task=task,
            stage_cfg=stage_cfg,
            plan_output=plan_output,
            human_instructions=human_instructions,
        )

    def _run_unstructured_edit(
        self,
        worker_tool: HeadlessCoderTool,
        stage_cfg,
        prompt: str,
    ) -> tuple[CoderResult, list[str]]:
        return do_work_stage.run_unstructured_edit(self, worker_tool, stage_cfg, prompt)

    def _mark_task_complete(
        self,
        task: TaskItem,
        *,
        summary: str,
        changed_files: list[str],
    ) -> None:
        return do_work_stage.mark_task_complete(
            self, task, summary=summary, changed_files=changed_files
        )

    def _mark_task_failed(
        self,
        task: TaskItem,
        *,
        error: str,
        changed_files: list[str],
    ) -> None:
        return do_work_stage.mark_task_failed(
            self, task, error=error, changed_files=changed_files
        )

    def _parallel_conflicts(
        self, outcomes: list[ParallelTaskOutcome]
    ) -> dict[int, list[str]]:
        return do_work_stage.parallel_conflicts(self, outcomes)

    def _parallel_batch_planner_enabled(self, stage_cfg) -> bool:
        return do_work_stage.parallel_batch_planner_enabled(self, stage_cfg)

    def _revision_replanner_enabled(self) -> bool:
        return do_work_stage.revision_replanner_enabled(self)

    def _execution_replanner_enabled(self) -> bool:
        return do_work_stage.execution_replanner_enabled(self)

    def _cross_task_success_replanner_enabled(self) -> bool:
        return do_work_stage.cross_task_success_replanner_enabled(self)

    def _ambiguous_success_replanner_enabled(self) -> bool:
        return do_work_stage.ambiguous_success_replanner_enabled(self)

    def _max_execution_replans(self) -> int:
        return do_work_stage.max_execution_replans(self)

    def _execution_replanning_count(self) -> int:
        return do_work_stage.execution_replanning_count(self)

    def _planning_tool_for_do_work(
        self, worker_tool: HeadlessCoderTool
    ) -> HeadlessCoderTool:
        return do_work_stage.planning_tool_for_do_work(self, worker_tool)

    def _build_batch_planning_prompt(
        self,
        *,
        ready_tasks: list[TaskItem],
        max_workers: int,
        plan_output: str,
        human_instructions: str | None = None,
    ) -> str:
        return do_work_stage.build_batch_planning_prompt(
            self,
            ready_tasks=ready_tasks,
            max_workers=max_workers,
            plan_output=plan_output,
            human_instructions=human_instructions,
        )

    def _collect_batch_file_hints(
        self,
        *,
        ready_by_id: dict[int, TaskItem],
        planned_tasks,
    ) -> dict[int, list[str]]:
        return do_work_stage.collect_batch_file_hints(
            self, ready_by_id=ready_by_id, planned_tasks=planned_tasks
        )

    def _planned_batch_preview(
        self,
        *,
        ready_by_id: dict[int, TaskItem],
        task_ids: list[int],
        hinted_files: dict[int, list[str]],
    ) -> list[TaskItem]:
        return do_work_stage.planned_batch_preview(
            self, ready_by_id=ready_by_id, task_ids=task_ids, hinted_files=hinted_files
        )

    def _ensure_conservative_planned_batch(self, batch: list[TaskItem]) -> None:
        return do_work_stage.ensure_conservative_planned_batch(self, batch)

    def _planned_execution_batch(
        self,
        *,
        worker_tool: HeadlessCoderTool,
        stage_cfg,
        plan_output: str,
        max_workers: int,
        human_instructions: str | None = None,
        execution_target_task_ids: list[int] | None = None,
    ) -> PlannedBatchSelection:
        return do_work_stage.planned_execution_batch(
            self,
            worker_tool=worker_tool,
            stage_cfg=stage_cfg,
            plan_output=plan_output,
            max_workers=max_workers,
            human_instructions=human_instructions,
            execution_target_task_ids=execution_target_task_ids,
        )

    def _revision_planning_worker_and_stage(
        self, do_work_worker: HeadlessCoderTool
    ) -> tuple[HeadlessCoderTool, Any]:
        return do_work_stage.revision_planning_worker_and_stage(self, do_work_worker)

    def _build_revision_replan_prompt(
        self,
        *,
        target_ids: set[int],
        downstream_ids: set[int],
    ) -> str:
        return do_work_stage.build_revision_replan_prompt(
            self, target_ids=target_ids, downstream_ids=downstream_ids
        )

    def _build_execution_replan_prompt(
        self,
        *,
        failed_task: TaskItem,
        error: str,
        changed_files: list[str],
        target_ids: set[int],
        downstream_ids: set[int],
    ) -> str:
        return do_work_stage.build_execution_replan_prompt(
            self,
            failed_task=failed_task,
            error=error,
            changed_files=changed_files,
            target_ids=target_ids,
            downstream_ids=downstream_ids,
        )

    def _cross_task_success_targets(
        self,
        *,
        source_task: TaskItem,
        changed_files: list[str],
    ) -> tuple[set[int], list[str]]:
        return do_work_stage.cross_task_success_targets(
            self, source_task=source_task, changed_files=changed_files
        )

    def _build_cross_task_success_replan_prompt(
        self,
        *,
        source_task: TaskItem,
        changed_files: list[str],
        target_ids: set[int],
        overlapping_files: list[str],
        downstream_ids: set[int],
    ) -> str:
        return do_work_stage.build_cross_task_success_replan_prompt(
            self,
            source_task=source_task,
            changed_files=changed_files,
            target_ids=target_ids,
            overlapping_files=overlapping_files,
            downstream_ids=downstream_ids,
        )

    def _ambiguous_success_reasons(
        self, *, task: TaskItem, changed_files: list[str]
    ) -> list[str]:
        return do_work_stage.ambiguous_success_reasons(
            self, task=task, changed_files=changed_files
        )

    def _build_ambiguous_success_replan_prompt(
        self,
        *,
        source_task: TaskItem,
        changed_files: list[str],
        reasons: list[str],
        target_ids: set[int],
        downstream_ids: set[int],
    ) -> str:
        return do_work_stage.build_ambiguous_success_replan_prompt(
            self,
            source_task=source_task,
            changed_files=changed_files,
            reasons=reasons,
            target_ids=target_ids,
            downstream_ids=downstream_ids,
        )

    def _task_definition_matches(self, current: TaskItem, replanned: TaskItem) -> bool:
        return do_work_stage.task_definition_matches(self, current, replanned)

    def _apply_replanned_tasks(
        self,
        *,
        plan: PlanOutput,
        target_ids: set[int],
        downstream_ids: set[int],
    ) -> None:
        return do_work_stage.apply_replanned_tasks(
            self, plan=plan, target_ids=target_ids, downstream_ids=downstream_ids
        )

    def _attempt_structured_revision_replan(
        self, do_work_worker: HeadlessCoderTool
    ) -> bool:
        return do_work_stage.attempt_structured_revision_replan(self, do_work_worker)

    def _attempt_structured_execution_replan(
        self,
        *,
        do_work_worker: HeadlessCoderTool,
        failed_task: TaskItem,
        error: str,
        changed_files: list[str],
    ) -> bool:
        return do_work_stage.attempt_structured_execution_replan(
            self,
            do_work_worker=do_work_worker,
            failed_task=failed_task,
            error=error,
            changed_files=changed_files,
        )

    def _attempt_structured_cross_task_success_replan(
        self,
        *,
        do_work_worker: HeadlessCoderTool,
        source_task: TaskItem,
        changed_files: list[str],
    ) -> bool:
        return do_work_stage.attempt_structured_cross_task_success_replan(
            self,
            do_work_worker=do_work_worker,
            source_task=source_task,
            changed_files=changed_files,
        )

    def _attempt_structured_ambiguous_success_replan(
        self,
        *,
        do_work_worker: HeadlessCoderTool,
        source_task: TaskItem,
        changed_files: list[str],
    ) -> bool:
        return do_work_stage.attempt_structured_ambiguous_success_replan(
            self,
            do_work_worker=do_work_worker,
            source_task=source_task,
            changed_files=changed_files,
        )

    def _select_structured_execution_batch(
        self,
        *,
        worker_tool: HeadlessCoderTool,
        stage_cfg,
        plan_output: str,
        max_workers: int,
        human_instructions: str | None = None,
        execution_target_task_ids: list[int] | None = None,
    ) -> list[TaskItem]:
        return do_work_stage.select_structured_execution_batch(
            self,
            worker_tool=worker_tool,
            stage_cfg=stage_cfg,
            plan_output=plan_output,
            max_workers=max_workers,
            human_instructions=human_instructions,
            execution_target_task_ids=execution_target_task_ids,
        )

    def _run_structured_do_work(
        self,
        worker_tool: HeadlessCoderTool,
        stage_cfg,
        plan_output: str,
        human_instructions: str | None = None,
        execution_target_task_ids: list[int] | None = None,
    ) -> str:
        return do_work_stage.run_structured_do_work(
            self,
            worker_tool,
            stage_cfg,
            plan_output,
            human_instructions,
            execution_target_task_ids,
        )

    def do_work(self, plan_output: str) -> str:
        return do_work_stage.execute_do_work(self, plan_output)

    def review(self, work_summary: str) -> Literal["pass", "revise", "aborted"]:
        return review_stage.execute_review(self, work_summary)

    def process_revision(self, decision: str) -> str:
        return revision_stage.execute_process_revision(self, decision)

    def revise(self, decision: str) -> str:
        return cast(Any, self.process_revision)(decision)

    def _planned_task_review_context(self) -> str:
        return revision_stage.planned_task_review_context(self)

    def _review_target_summary(self) -> str:
        return revision_stage.review_target_summary(self)

    def _task_files_for_ids(self, task_ids: list[int]) -> list[str]:
        return revision_stage.task_files_for_ids(self, task_ids)

    def _task_ids_for_file_selector(self, raw_selector: str) -> list[int]:
        return revision_stage.task_ids_for_file_selector(self, raw_selector)

    def _hinted_task_ids(self) -> list[int]:
        return revision_stage.hinted_task_ids(self)

    def _prepare_structured_revision_prompt(self) -> str:
        return revision_stage.prepare_structured_revision_prompt(self)

    def _build_structured_revision_prompt(self, targeted_ids: list[int]) -> str:
        return revision_stage.build_structured_revision_prompt(self, targeted_ids)

    def _revision_target_line(self, task: TaskItem) -> str:
        return revision_stage.revision_target_line(self, task)

    def _mark_tasks_for_revision(self) -> list[int]:
        return revision_stage.mark_tasks_for_revision(self)

    def _collect_target_task_ids(self) -> set[int]:
        return revision_stage.collect_target_task_ids(self)

    def _build_revision_notes_by_task(
        self, target_ids: set[int]
    ) -> dict[int, list[str]]:
        return revision_stage.build_revision_notes_by_task(self, target_ids)

    def _normalized_review_task_hints(self) -> list[ReviewTaskHint]:
        return revision_stage.normalized_review_task_hints(self)

    def _infer_review_task_hints(
        self, decision: ReviewCrewDecision
    ) -> list[ReviewTaskHint]:
        return revision_stage.infer_review_task_hints(self, decision)

    def _expand_dependent_task_ids(self, seed_ids: set[int]) -> set[int]:
        return revision_stage.expand_dependent_task_ids(self, seed_ids)

    def finalize(self, _decision: str) -> str:
        return finalize_stage.execute_finalize(self, _decision)

    def _delivery_verification_ok(self) -> bool:
        return finalize_stage.delivery_verification_ok(self)

    def _run_pre_delivery_verification(self) -> VerificationReport | None:
        return finalize_stage.run_pre_delivery_verification(self)

    def _delivery_verification_note(self, verification_ok: bool) -> str:
        return finalize_stage.delivery_verification_note(self, verification_ok)

    def _maybe_deliver(self, extra_changed: list[str]) -> None:
        return finalize_stage.maybe_deliver(self, extra_changed)

    def handle_aborted(self, _decision: str) -> str:
        return terminal_stage.execute_handle_aborted(self, _decision)

    def handle_failed(self, _decision: str) -> str:
        return terminal_stage.execute_handle_failed(self, _decision)


def build_headless_flow(
    *,
    config: FlowConfig | None = None,
    run_store: RunStore | None = None,
    config_dir: Path | str | None = None,
) -> CrewAIHeadlessFlow:
    """Build a Flow whose topology comes from ``config/flow.yaml``.

    Constructs ``CrewAIHeadlessFlow`` (workers/HITL/verify helpers intact), then
    rebinds stage bodies from the declarative definition (ADR-0012 Phase 3).
    """
    flow = CrewAIHeadlessFlow(config=config, run_store=run_store)
    definition = load_flow_definition(config_dir=config_dir)
    flow._definition = definition
    flow._methods = flow._action_bound_methods()
    for name, method in flow._methods.items():
        setattr(flow, name, method)
    flow._skip_auto_memory = True
    flow.suppress_flow_events = True
    if definition.config.max_method_calls is not None:
        flow.max_method_calls = definition.config.max_method_calls
    return flow


# Back-compat alias for Phase 2 equivalence helpers / older imports.
build_topology_twin_flow = build_headless_flow


# Convenience runner for demos / CLI
def run_headless_flow(
    request: str,
    target_repo: str,
    max_revisions: int = 2,
    config: FlowConfig | None = None,
    runs_dir: str | Path | None = None,
    config_dir: str | Path | None = None,
) -> FlowState:
    """
    High-level entry point for running the full flow.
    """
    state = FlowState(
        request=request,
        target_repo=target_repo,
        max_revisions=max_revisions,
        config_dir=str(Path(config_dir).resolve()) if config_dir else None,
    )

    run_store: RunStore | None = None
    if runs_dir is not None:
        run_store = RunStore.allocate(Path(runs_dir), request)
        state.run_id = run_store.run_id
        state.run_dir = str(run_store.run_dir)
        state.created_at = datetime.now().isoformat(timespec="seconds")

    flow = build_headless_flow(
        config=config, run_store=run_store, config_dir=config_dir
    )
    flow.kickoff(inputs=state.model_dump())

    return flow.state


def synthesize_crash_checkpoint(state: FlowState) -> AbortedCheckpoint:
    """
    Map a crashed run (status still "running" on disk) onto a resumable
    checkpoint.

    ``last_stage`` is not "last completed stage": structured do_work and
    review both set it *before* their worker calls. The mapping therefore
    re-runs the named stage rather than advancing past it — replay is safe
    because done tasks stay done and review is read-only.
    """
    last_stage = state.last_stage
    if last_stage is None:
        return AbortedCheckpoint(stage="plan")
    if last_stage == "plan":
        # Plan committed its output; continue with do_work (plan_output is
        # rebuilt from state.spec/state.tasks by the resume machinery).
        return AbortedCheckpoint(stage="do_work")
    if last_stage == "do_work":
        return AbortedCheckpoint(stage="do_work")
    if last_stage == "review":
        if state.latest_work_summary:
            return AbortedCheckpoint(
                stage="review", stage_input=state.latest_work_summary
            )
        return AbortedCheckpoint(stage="do_work")
    if last_stage == "finalize":
        return AbortedCheckpoint(stage="finalize", stage_input="pass")
    raise ValueError(
        f"Cannot resume crashed run from last_stage={last_stage!r}. "
        "Recoverable stages: plan, do_work, review, finalize (or none)"
    )


def _resolve_resume_run_store(
    state: FlowState, runs_dir: str | Path | None
) -> RunStore | None:
    if state.run_dir and Path(state.run_dir).is_dir():
        return RunStore.attach(state.run_dir)
    if runs_dir is None:
        return None
    if state.run_id:
        run_dir = Path(runs_dir) / state.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        run_store = RunStore(run_dir)
    else:
        run_store = RunStore.allocate(Path(runs_dir), state.request)
        state.run_id = run_store.run_id
    state.run_dir = str(run_store.run_dir)
    return run_store


def resume_headless_flow(
    state: FlowState,
    config: FlowConfig | None = None,
    runs_dir: str | Path | None = None,
) -> FlowState:
    """
    Resume a previously aborted flow from the last human gate, or a crashed
    run (status still "running") from its last checkpoint.
    """
    crashed = state.status == "running"
    if state.status not in {"aborted_by_human", "running"}:
        raise ValueError(
            "Can only resume a flow with status 'aborted_by_human' (human "
            "gate) or 'running' (crashed run)"
        )

    checkpoint: AbortedCheckpoint | None
    if crashed:
        checkpoint = synthesize_crash_checkpoint(state)
        # Attempts interrupted mid-flight are unfinished, not done.
        for task in state.tasks:
            if task.status == "in_progress":
                task.status = "pending"
    else:
        checkpoint = state.aborted_checkpoint
    if checkpoint is None or checkpoint.stage not in {
        "plan",
        "do_work",
        "review",
        "finalize",
    }:
        raise ValueError(
            "Can only resume flows aborted before 'plan', 'do_work', 'review', or 'finalize'"
        )

    resume_stage = checkpoint.stage
    resume_gate = checkpoint.gate
    resume_message = checkpoint.message
    resume_before_review_instructions = checkpoint.before_review_instructions
    resume_input = checkpoint.stage_input
    if not crashed and resume_gate is None and resume_stage == "review":
        for entry in reversed(state.human_feedback_log):
            feedback = (
                entry
                if isinstance(entry, HumanFeedbackEntry)
                else HumanFeedbackEntry.model_validate(entry)
            )
            if feedback.stage == "review" and feedback.approved is False:
                resume_gate = feedback.gate
                break

    run_store = _resolve_resume_run_store(state, runs_dir)
    flow = build_headless_flow(
        config=config,
        run_store=run_store,
        config_dir=state.config_dir,
    )
    flow._state = state  # type: ignore[attr-defined]
    flow.state.status = "running"
    flow.state.clear_aborted_checkpoint()
    flow._refresh_debug_report()
    direct_flow = cast(Any, flow)

    def continue_from_review_decision(decision: str) -> FlowState:
        current_decision = decision
        if flow.state.status in {"completed", "aborted_by_human", "failed"}:
            return flow.state

        while current_decision == "revise":
            revise_prompt = direct_flow.revise(current_decision)
            if flow.state.status in {"completed", "aborted_by_human", "failed"}:
                return flow.state
            work_summary = direct_flow.do_work(revise_prompt)
            if flow.state.status in {"completed", "aborted_by_human", "failed"}:
                return flow.state
            current_decision = direct_flow.review(work_summary)
            if flow.state.status in {"completed", "aborted_by_human", "failed"}:
                return flow.state

        if current_decision == "pass":
            direct_flow.finalize(current_decision)

        return flow.state

    if crashed and resume_stage == "finalize" and state.review_status == "revise":
        # Force-revise-then-crash window: the human already reopened the work
        # before finalize; continue the revise loop instead of re-finalizing.
        return continue_from_review_decision("revise")

    if resume_stage == "finalize":
        return continue_from_review_decision(
            direct_flow.finalize(resume_input or "pass")
        )

    if resume_stage == "plan":
        plan_output = direct_flow.plan()
        if flow.state.status == "aborted_by_human":
            return flow.state
    elif resume_stage == "do_work":
        plan_output = resume_input or render_plan_markdown(
            state_items_to_plan_output(flow.state.spec, flow.state.tasks)
        )
    else:
        plan_output = None

    if resume_stage in {"plan", "do_work"}:
        work_summary = direct_flow.do_work(plan_output)
    elif resume_stage == "review":
        if not resume_input:
            raise ValueError(
                "Resume state missing saved input for aborted 'review' gate"
            )
        work_summary = resume_input
    else:
        work_summary = None
    if flow.state.status == "aborted_by_human":
        return flow.state

    if work_summary is None:
        raise ValueError("Resume state could not recover review input")

    if is_after_review_gate(resume_stage, resume_gate):
        decision = direct_flow._resume_after_review_checkpoint(
            work_summary,
            saved_message=resume_message,
            saved_before_review_instructions=resume_before_review_instructions,
        )
    else:
        decision = direct_flow.review(work_summary)
    return continue_from_review_decision(decision)
