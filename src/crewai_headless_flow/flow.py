"""
The main reusable CrewAI Flow for multi-agent headless coding with pluggable workers.

Topology (as specified):
- @start plan          → structured planning via configured worker or optional Planning Crew
- @listen do_work      → configured worker (edit mode) + implementation skill
- @router review       → configured worker (inspect mode) + review + doubt skills → "pass" | "revise"
- @listen("revise")    → bounded loop back to do_work
- @listen("pass")      → finalize with documentation skill
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal, cast

from crewai.flow.flow import Flow, listen, router, start

from .config import (
    FlowConfig,
    StageConfig,
    classify_stage_extra,
    get_default_config,
)
from .do_work_batch_contract import (
    DO_WORK_BATCH_PLAN_SCHEMA,
    normalize_do_work_batch_plan,
    require_concrete_do_work_batch_plan,
)
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
from .delivery import deliver
from .do_work_crew import run_do_work_crew
from .escalation import (
    EscalationHandler,
    EscalationRequest,
    get_handler as get_escalation_handler,
)
from .plan_contract import (
    PlanOutput,
    normalize_plan_output,
    plan_tasks_to_state_items,
    require_concrete_plan,
    render_plan_markdown,
    state_items_to_plan_output,
)
from .plan_crew import run_plan_crew
from .review_contract import (
    REVIEW_DECISION_SCHEMA,
    ReviewDecision,
    ReviewTaskHint,
    normalize_review_output,
)
from .review_crew import ReviewCrewDecision, run_review_crew
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
    TaskExecutionEntry,
    TriggerReason,
)
from .task_batches import (
    has_pending_tasks,
    ready_execution_tasks,
    select_execution_batch,
)
from .paths_policy import match_denied, restore_denied_paths
from .tools.coder_tool import HeadlessCoderTool
from .verification import VerificationReport, VerifyRunner, run_verification
from .workers import WORKER_SPECS
from .workers.base import CoderResult, HeadlessCoder
from .workspace_changes import (
    apply_changed_files,
    cleanup_workspace_copy,
    create_workspace_copy,
    diff_workspace_snapshots,
    snapshot_workspace,
)


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


@dataclass
class ParallelTaskOutcome:
    task: TaskItem
    summary: str
    success: bool
    changed_files: list[str]
    error: str | None = None
    workspace: str | None = None


@dataclass
class PlannedBatchSelection:
    task_ids: list[int]
    hinted_files: dict[int, list[str]]
    summary: str
    planned_files: list[str]


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
        """Build an adapter, honoring a configured binary override.

        Every adapter accepts ``binary=``; the zero-arg call keeps their
        defaults when no override is configured.
        """
        settings = getattr(self.config, "worker_settings", {}) or {}
        binary = (settings.get(worker_name) or {}).get("binary")
        if binary:
            return adapter_cls(binary=binary)  # type: ignore[call-arg]
        return adapter_cls()

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
        issue_lines = (
            "\n".join(f"- {issue}" for issue in decision.issues[:5])
            if decision.issues
            else "- None"
        )
        review_targets = self._review_target_summary()
        review_target_block = (
            f"Suggested review targets:\n{review_targets}\n\n"
            if review_targets != "- None"
            else ""
        )
        task_catalog = (
            f"\nCurrent task graph:\n{self._current_task_graph_summary()}\n"
            if self.state.tasks
            else ""
        )
        return (
            "Automated review completed.\n"
            f"Suggested status: {decision.status}\n"
            f"Summary: {decision.summary}\n"
            f"Issues ({len(decision.issues)}):\n{issue_lines}\n\n"
            f"{review_target_block}"
            f"{task_catalog}"
            "Approve to accept this review decision, abort to stop here, or use an enabled review override."
        )

    def _build_review_prompt(
        self,
        *,
        work_summary: str,
        human_guidance: str | None = None,
        review_rerun_guidance: str | None = None,
        verification_evidence: str | None = None,
    ) -> str:
        rerun_guidance = (
            f"\nHuman rerun instructions:\n- {review_rerun_guidance}\n"
            if review_rerun_guidance
            else ""
        )
        human_guidance_text = (
            f"\nHuman approval instructions:\n- {human_guidance}\n"
            if human_guidance
            else ""
        )
        verification_text = (
            f"\nObjective verification evidence:\n{verification_evidence}\n"
            if verification_evidence
            else ""
        )
        return f"""You are performing a rigorous code review following the assigned procedure.

Work that was just performed:
{work_summary}

Original request:
{self.state.request}

Changed files so far: {self.state.changed_files}

Planned tasks:
{self._planned_task_review_context()}
{verification_text}{human_guidance_text}{rerun_guidance}

Respond with a single JSON object ONLY (no other text):

{{
  "status": "pass" or "revise",
  "issues": [
    "specific issue 1",
    "specific issue 2"
  ],
  "summary": "one sentence overall assessment",
  "task_hints": [
    {{
      "task_ids": [1],
      "files": ["src/example.py"],
      "summary": "why this task should be revised"
    }}
  ]
}}

If everything looks good according to the review procedure, use "pass".
Otherwise use "revise" and list the concrete issues that must be addressed.
Use `task_hints` when you can map an issue to planned tasks or likely files. Use an empty list when you cannot map confidently.
"""

    def _run_verification_round(self) -> VerificationReport | None:
        """Run the operator-declared verify commands for this review round.

        Returns None when no commands are configured. Runs once per entry
        into the review loop, so every revise cycle (and a human
        rerun-review) re-verifies the tree.
        """

        verify_cfg = self.config.verify
        if not verify_cfg.get("commands"):
            return None

        self.state.last_stage = "review"
        # Verify commands are the longest-running Flow-owned subprocesses
        # (test suites, builds) and the likeliest place for an operator
        # Ctrl-C or machine crash; checkpoint first so a resume lands at
        # the review stage instead of replaying completed work.
        self._refresh_debug_report()
        logger.info(
            f"[Flow] Running {len(verify_cfg['commands'])} verification "
            f"command(s) in {self.state.target_repo}..."
        )
        report = run_verification(
            verify_cfg,
            cwd=self.state.target_repo,
            runner=self._verification_runner,
        )
        report.revision = self.state.revisions
        self.state.verification_runs.append(report)
        outcome = "passed" if report.passed else "FAILED"
        log = logger.info if report.passed else logger.warning
        log(f"[Flow] Verification {outcome}: {report.message}")
        self._log_event(
            "verification",
            passed=report.passed,
            mode=report.mode,
            commands=len(report.results),
            message=report.message,
        )
        self._refresh_debug_report()
        return report

    def _verification_revise_decision(
        self, report: VerificationReport
    ) -> ReviewDecision:
        issues: list[str] = []
        for result in report.results:
            if result.exit_code == 0:
                continue
            suffix = " (timed out)" if result.timed_out else ""
            issue = (
                f"Verification command `{result.command}` exited "
                f"{result.exit_code}{suffix}"
            )
            tail = result.output_tail.strip()
            if tail:
                issue += f":\n{tail}"
            issues.append(issue)
        return ReviewDecision(
            status="revise",
            issues=issues,
            summary="Objective verification failed; automated review skipped.",
            task_hints=[],
        )

    def _render_verification_evidence(
        self, report: VerificationReport | None
    ) -> str | None:
        if report is None:
            return None
        header = "passed" if report.passed else "FAILED"
        lines = [f"Verification commands ({header}):"]
        for result in report.results:
            if result.timed_out:
                status = "timed out"
            elif result.exit_code == 0:
                status = "ok"
            else:
                status = f"exit {result.exit_code}"
            lines.append(f"- `{result.command}` — {status}")
            tail = result.output_tail.strip()
            if result.exit_code != 0 and tail:
                lines.extend(f"  {tail_line}" for tail_line in tail.splitlines())
        return "\n".join(lines)

    def _record_review_decision_state(self, decision: ReviewDecision) -> None:
        status = decision.status
        issues = decision.issues
        self.state.review_status = status
        self.state.issues = issues
        review_task_hints = decision.task_hints
        if not review_task_hints and status == "revise" and self.state.tasks:
            review_task_hints = self._infer_review_task_hints(decision)
        self.state.review_task_hints = review_task_hints
        self._refresh_debug_report()
        self._record_history(
            kind="review_decision",
            summary=f"Review returned {status}.",
            task_ids=sorted(
                {
                    task_id
                    for hint in review_task_hints
                    for task_id in getattr(hint, "task_ids", [])
                }
            ),
            files=sorted(
                {
                    path
                    for hint in review_task_hints
                    for path in getattr(hint, "files", [])
                }
            ),
            details=[decision.summary, *issues[:3]],
        )

    def _fail_closed_for_incomplete_structured_tasks(
        self, decision: ReviewDecision
    ) -> ReviewDecision:
        if decision.status != "pass" or not self.state.tasks:
            return decision

        incomplete_task_ids = [
            task.id for task in self.state.tasks if task.status != "done"
        ]
        if not incomplete_task_ids:
            return decision

        issue = "Structured tasks remain incomplete: " + ", ".join(
            str(task_id) for task_id in incomplete_task_ids
        )
        return ReviewDecision(
            status="revise",
            issues=[*decision.issues, issue],
            summary="Automated review cannot pass while structured tasks remain incomplete.",
            task_hints=[
                *decision.task_hints,
                ReviewTaskHint(
                    task_ids=incomplete_task_ids,
                    files=self._task_files_for_ids(incomplete_task_ids),
                    summary="Complete the remaining structured tasks before review can pass.",
                ),
            ],
        )

    def _run_automated_review_once(
        self,
        *,
        work_summary: str,
        human_guidance: str | None = None,
        review_rerun_guidance: str | None = None,
        verification_evidence: str | None = None,
    ) -> ReviewDecision:
        worker_tool = self._get_worker("review")
        stage_cfg = self.config.get_stage("review")
        prompt = self._build_review_prompt(
            work_summary=work_summary,
            human_guidance=human_guidance,
            review_rerun_guidance=review_rerun_guidance,
            verification_evidence=verification_evidence,
        )

        self.state.last_stage = "review"
        crew_cfg = stage_cfg.extra.get("crew", {}) or {}
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
        else:
            result = worker_tool.run(
                task=prompt,
                cwd=self.state.target_repo,
                mode="inspect",  # Critical: read-only guarantee
                schema=REVIEW_DECISION_SCHEMA,
                timeout=stage_cfg.timeout,
                model=stage_cfg.model,
            )
            decision = normalize_review_output([result.raw_output, result.summary])

        decision = self._fail_closed_for_incomplete_structured_tasks(decision)
        self._record_review_decision_state(decision)
        return decision

    def _run_verified_review_once(
        self,
        *,
        work_summary: str,
        human_guidance: str | None = None,
        review_rerun_guidance: str | None = None,
    ) -> ReviewDecision:
        """One verification round paired with one automated review.

        Every path that re-reviews the tree (the main review loop, a
        rerun-review from the resume path, a rerun-review at the finalize
        gate) must go through here so a review decision can never be
        produced against an unverified tree. A gate-mode verification
        failure skips the LLM entirely: the command output IS the review,
        fed into the revise loop through the same funnel.
        """
        verification = self._run_verification_round()
        if (
            verification is not None
            and not verification.passed
            and verification.mode == "gate"
        ):
            decision = self._verification_revise_decision(verification)
            self._record_review_decision_state(decision)
            return decision
        return self._run_automated_review_once(
            work_summary=work_summary,
            human_guidance=human_guidance,
            review_rerun_guidance=review_rerun_guidance,
            verification_evidence=self._render_verification_evidence(verification),
        )

    def _handle_after_review_checkpoint(
        self,
        *,
        work_summary: str,
        message: str,
        automated_status: Literal["pass", "revise"],
        human_guidance: str | None = None,
    ) -> tuple[Literal["pass", "revise", "aborted", "rerun-review"], str | None]:
        after_review_decision = self._maybe_ask_human(
            "review",
            message,
            gate="after_review",
        )
        if not after_review_decision.proceed:
            if after_review_decision.action == "force-revise":
                logger.info("[Flow] Human forced review revise after automated review.")
                self.state.last_stage = "review"
                self.state.review_status = "revise"
                if automated_status == "revise":
                    if after_review_decision.instructions:
                        self.state.issues = [
                            *self.state.issues,
                            f"Human review note: {after_review_decision.instructions}",
                        ]
                else:
                    self.state.issues = [
                        after_review_decision.instructions
                        or "Human requested revision after automated review."
                    ]
                    self.state.review_task_hints = []
                self._refresh_debug_report()
                self._record_history(
                    kind="review_decision",
                    summary="Human overrode automated review to revise.",
                    details=[
                        f"Automated review was {automated_status}.",
                        *(self.state.issues[:3]),
                    ],
                )
                return "revise", None
            if after_review_decision.action == "replan":
                logger.info("[Flow] Human requested replanning after automated review.")
                reason = self._set_pending_revision_replan(
                    after_review_decision.instructions
                )
                self.state.last_stage = "review"
                self.state.review_status = "revise"
                if automated_status == "revise":
                    self.state.issues = [
                        *self.state.issues,
                        f"Human replan note: {reason}",
                    ]
                else:
                    self.state.issues = [reason]
                    self.state.review_task_hints = []
                self._refresh_debug_report()
                self._record_history(
                    kind="human_replanning",
                    summary="Human requested replanning after automated review.",
                    details=[f"Automated review was {automated_status}.", reason],
                )
                return "revise", None
            if after_review_decision.action == "force-pass":
                logger.info("[Flow] Human forced review pass after automated review.")
                self.state.last_stage = "review"
                self.state.review_status = "pass"
                self.state.issues = []
                self.state.review_task_hints = []
                self._refresh_debug_report()
                self._record_history(
                    kind="review_decision",
                    summary="Human overrode automated review to pass.",
                    details=[f"Automated review was {automated_status}."],
                )
                return "pass", None
            if after_review_decision.action == "target-tasks":
                logger.info("[Flow] Human selected targeted revision tasks.")
                selected_ids = after_review_decision.task_ids or []
                reason = (
                    after_review_decision.instructions
                    or "Human selected tasks for targeted revision after automated review."
                )
                self.state.last_stage = "review"
                self.state.review_status = "revise"
                if automated_status == "revise":
                    self.state.issues = [
                        *self.state.issues,
                        f"Human target note: {reason}",
                    ]
                else:
                    self.state.issues = [reason]
                self.state.review_task_hints = [
                    ReviewTaskHint(
                        task_ids=selected_ids,
                        files=self._task_files_for_ids(selected_ids),
                        summary=reason,
                    )
                ]
                self._refresh_debug_report()
                self._record_history(
                    kind="revision_targeting",
                    summary="Human selected targeted revision tasks after automated review.",
                    task_ids=selected_ids,
                    files=self._task_files_for_ids(selected_ids),
                    details=[f"Automated review was {automated_status}.", reason],
                )
                return "revise", None
            if after_review_decision.action == "rerun-review":
                logger.info("[Flow] Human requested automated review rerun.")
                review_rerun_guidance = (
                    after_review_decision.instructions
                    or "Human requested automated review rerun."
                )
                self._record_history(
                    kind="review_decision",
                    summary="Human requested automated review rerun.",
                    details=[
                        f"Automated review was {automated_status}.",
                        review_rerun_guidance,
                    ],
                )
                return "rerun-review", review_rerun_guidance
            logger.info("[Flow] Human aborted after review decision.")
            self._mark_human_abort(
                "review",
                stage_input=work_summary,
                gate="after_review",
                message=message,
                before_review_instructions=human_guidance,
            )
            return "aborted", None
        if after_review_decision.instructions:
            if automated_status == "revise":
                self.state.issues = [
                    *self.state.issues,
                    f"Human review note: {after_review_decision.instructions}",
                ]
                self._record_history(
                    kind="review_decision",
                    summary="Human approved automated review with extra revise guidance.",
                    details=[after_review_decision.instructions],
                )
                self._refresh_debug_report()
            else:
                self._record_history(
                    kind="review_decision",
                    summary="Human approved automated review pass with extra guidance.",
                    details=[after_review_decision.instructions],
                )
        return automated_status, None

    def _resume_after_review_checkpoint(
        self,
        work_summary: str,
        *,
        saved_message: str | None = None,
        saved_before_review_instructions: str | None = None,
    ) -> Literal["pass", "revise", "aborted"]:
        direct_flow = cast(Any, self)
        self.state.latest_work_summary = work_summary
        after_review_message = saved_message or self.state.aborted_gate_message
        if after_review_message is None:
            aborted_entry = self._latest_human_feedback_entry(
                stage="review",
                gate="after_review",
                approved=False,
            )
            if aborted_entry is None:
                return direct_flow.review(work_summary)
            after_review_message = aborted_entry.message

        automated_status = self.state.review_status
        if automated_status not in {"pass", "revise"}:
            return direct_flow.review(work_summary)

        human_guidance = (
            saved_before_review_instructions
            or self.state.aborted_before_review_instructions
        )
        if human_guidance is None:
            before_review_entry = self._latest_human_feedback_entry(
                stage="review",
                gate="before_review",
                approved=True,
            )
            human_guidance = (
                before_review_entry.instructions
                if before_review_entry is not None
                else None
            )
        review_rerun_guidance: str | None = None

        while True:
            result, review_rerun_guidance = self._handle_after_review_checkpoint(
                work_summary=work_summary,
                message=after_review_message,
                automated_status=cast(Literal["pass", "revise"], automated_status),
                human_guidance=human_guidance,
            )
            if result != "rerun-review":
                return cast(Literal["pass", "revise", "aborted"], result)

            decision = self._run_verified_review_once(
                work_summary=work_summary,
                human_guidance=human_guidance,
                review_rerun_guidance=review_rerun_guidance,
            )
            automated_status = decision.status
            logger.info(
                f"[Flow] Review decision: {automated_status} | Issues: {len(decision.issues)}"
            )
            after_review_message = self._after_review_message(decision)

    # ------------------------------------------------------------------
    # Planning stage
    # ------------------------------------------------------------------
    def _plan_crew_enabled(self, stage_cfg) -> bool:
        crew_cfg = stage_cfg.extra.get("crew", {}) or {}
        return bool(crew_cfg.get("enabled", False))

    def _current_task_graph_summary(self) -> str:
        if not self.state.tasks:
            return "- No current structured tasks"
        lines = []
        for task in self.state.tasks:
            files = ", ".join(task.files) if task.files else "unknown"
            lines.append(
                f"- Task {task.id}: {task.title or task.description} "
                f"| status={task.status} | files={files}"
            )
        return "\n".join(lines)

    def _build_plan_prompt(
        self,
        human_instructions: str | None = None,
        *,
        current_plan_output: str | None = None,
        replanning_reason: str | None = None,
    ) -> str:
        human_guidance = (
            f"\nHuman approval instructions:\n- {human_instructions}\n"
            if human_instructions
            else ""
        )
        current_plan_context = ""
        if current_plan_output or self.state.tasks:
            current_plan_context = (
                "\nCurrent saved plan/task graph:\n"
                f"{self._current_task_graph_summary()}\n"
            )
            if current_plan_output:
                current_plan_context += (
                    f"\nCurrent rendered plan markdown:\n{current_plan_output[:3000]}\n"
                )
        replanning_context = (
            f"\nReplanning context:\n- {replanning_reason}\n"
            if replanning_reason
            else ""
        )
        return f"""Create a structured implementation plan for this repository request.

Original user request:
{self.state.request}

Target repository: {self.state.target_repo}

Inspect relevant files as needed so the plan is grounded in the actual codebase.
{current_plan_context}{replanning_context}{human_guidance}

Produce a single structured plan with:
- `spec`: concise but complete objective, success criteria, and boundaries
- `tasks`: small vertical slices with explicit acceptance criteria, verification, dependencies, likely files, and estimated scope
""".strip()

    def _execute_plan_stage(
        self,
        *,
        human_instructions: str | None = None,
        current_plan_output: str | None = None,
        replanning_reason: str | None = None,
    ) -> str:
        stage_cfg = self.config.get_stage("plan")
        worker_tool = self._get_worker("plan")
        prompt = self._build_plan_prompt(
            human_instructions,
            current_plan_output=current_plan_output,
            replanning_reason=replanning_reason,
        )

        if self._plan_crew_enabled(stage_cfg):
            try:
                plan = run_plan_crew(
                    planning_context=prompt,
                    worker_tool=worker_tool,
                    cwd=self.state.target_repo,
                    timeout=stage_cfg.timeout,
                    model=stage_cfg.model,
                    crew_config=stage_cfg.extra.get("crew", {}) or {},
                )
            except Exception as exc:
                raise RuntimeError(f"Planning Crew failed: {exc}") from exc
        else:
            result = worker_tool.run(
                task=prompt,
                cwd=self.state.target_repo,
                mode="inspect",
                schema=PlanOutput.model_json_schema(),
                model=stage_cfg.model,
                timeout=stage_cfg.timeout,
            )
            if not result.success:
                error = result.error or result.summary or result.raw_output
                raise RuntimeError(f"Planning stage failed: {error}")

            plan = normalize_plan_output([result.summary, result.raw_output])

        try:
            plan = require_concrete_plan(plan)
        except ValueError as exc:
            raise RuntimeError(
                f"Planning stage returned an invalid structured plan: {exc}"
            ) from exc

        output = render_plan_markdown(plan)

        self.state.spec = plan.spec
        self.state.tasks = plan_tasks_to_state_items(plan.tasks)
        self.state.last_stage = "plan"
        self._refresh_debug_report()

        logger.info(
            f"\n[Flow] Planning complete. "
            f"Spec length: {len(plan.spec)} | Tasks: {len(plan.tasks)}"
        )
        return output

    @start()
    def plan(self) -> str:
        self._mark_running()
        self._log_event("stage_start", stage="plan")
        self.config.print_mapping()  # Visibility into current wiring
        human_gate_message = (
            "About to run planning stage (plan). "
            "This is read-only but may inspect a broad slice of the repository."
        )
        decision = self._maybe_ask_human(
            "plan",
            human_gate_message,
        )
        if not decision.proceed:
            logger.info("[Flow] Human aborted before plan.")
            self._mark_human_abort("plan", message=human_gate_message)
            return "aborted-by-human"
        return self._execute_plan_stage(human_instructions=decision.instructions)

    # ------------------------------------------------------------------
    # Core work stage - delegates to the configured headless coder (edit mode)
    # ------------------------------------------------------------------
    def _parallel_do_work_enabled(self, stage_cfg) -> bool:
        parallel_cfg = stage_cfg.extra.get("parallel", {}) or {}
        return bool(parallel_cfg.get("enabled", False))

    def _do_work_crew_enabled(self, stage_cfg) -> bool:
        crew_cfg = stage_cfg.extra.get("crew", {}) or {}
        return bool(crew_cfg.get("enabled", False))

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
        acceptance = "\n".join(
            f"- {criterion}" for criterion in task.acceptance_criteria
        )
        verification = "\n".join(f"- {check}" for check in task.verification)
        files = "\n".join(f"- {path}" for path in task.files)
        dependencies = ", ".join(str(dep) for dep in task.dependencies) or "None"
        review_notes = "\n".join(f"- {note}" for note in task.review_notes)
        human_guidance = (
            f"\nHuman approval instructions:\n- {human_instructions}\n"
            if human_instructions
            else ""
        )

        return f"""Follow the assigned operating procedure for this implementation stage.

Plan / spec context:
{plan_output[:3000]}

Original user request:
{self.state.request}

Target repo: {self.state.target_repo}

Current revision count: {self.state.revisions}

Task:
- Id: {task.id}
- Title: {task.title or task.description}
- Description: {task.description}
- Current task status: {task.status}
- Dependencies satisfied: {dependencies}

Acceptance criteria:
{acceptance or "- None provided"}

Verification:
{verification or "- None provided"}

Files likely touched:
{files or "- None provided"}

Review notes for this task:
{review_notes or "- None provided"}
{human_guidance}

Work only on this task unless an additional file change is required to keep the repo coherent.
After you are done, summarize what changed and whether task verification now passes.
""".strip()

    def _track_changed_files(self, changed_files: list[str]) -> None:
        seen = set(self.state.changed_files)
        for path in changed_files:
            normalized = path.strip()
            if not normalized or normalized in seen:
                continue
            self.state.changed_files.append(normalized)
            seen.add(normalized)

    def _next_task_attempt(self, task_id: int) -> int:
        attempts = 0
        for entry in self.state.task_executions:
            candidate = (
                entry
                if isinstance(entry, TaskExecutionEntry)
                else TaskExecutionEntry.model_validate(entry)
            )
            if candidate.task_id == task_id:
                attempts += 1
        return attempts + 1

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
        target_repo = str(Path(self.state.target_repo).resolve(strict=False))
        resolved_cwd = str(Path(cwd).resolve(strict=False))
        isolated_workspace = resolved_cwd != target_repo
        self.state.task_executions.append(
            TaskExecutionEntry(
                task_id=task.id,
                attempt=self._next_task_attempt(task.id),
                revision=self.state.revisions,
                worker=stage_cfg.worker,
                model=stage_cfg.model,
                orchestration=(
                    "crew" if self._do_work_crew_enabled(stage_cfg) else "direct"
                ),
                success=result.success,
                summary=result.summary or result.raw_output,
                error=result.error,
                changed_files=changed_files,
                isolated_workspace=isolated_workspace,
                workspace=resolved_cwd if isolated_workspace else None,
                parallel_batch_id=parallel_batch_id,
                crew_rounds=crew_rounds,
            )
        )
        self._refresh_debug_report()

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
        before = snapshot_workspace(Path(cwd))
        task_prompt = self._build_task_execution_prompt(
            task,
            plan_output,
            human_instructions=human_instructions,
        )
        crew_rounds: list[CrewRoundEntry] = []
        if self._do_work_crew_enabled(stage_cfg):
            crew_cfg = stage_cfg.extra.get("crew", {}) or {}
            try:
                result, decision = run_do_work_crew(
                    task_prompt=task_prompt,
                    worker_tool=worker_tool,
                    cwd=cwd,
                    timeout=stage_cfg.timeout,
                    model=stage_cfg.model,
                    crew_config=crew_cfg,
                    round_observer=lambda event: crew_rounds.append(
                        CrewRoundEntry.model_validate(event)
                    ),
                )
            except Exception as exc:
                result = CoderResult(
                    summary="",
                    raw_output="",
                    exit_code=1,
                    error=f"Implementation Crew failed: {exc}",
                )
            else:
                if result.success and decision.status != "pass":
                    issues = "; ".join(decision.issues) or decision.summary
                    result = CoderResult(
                        summary=decision.summary or result.summary,
                        changed_files=result.changed_files,
                        tests_passed=result.tests_passed,
                        raw_output=result.raw_output,
                        exit_code=1,
                        error=f"Implementation Crew requested revision: {issues}",
                    )
                elif result.success and decision.summary.strip():
                    result = CoderResult(
                        summary=decision.summary,
                        changed_files=result.changed_files,
                        tests_passed=result.tests_passed,
                        raw_output=result.raw_output,
                        exit_code=result.exit_code,
                        error=result.error,
                    )
        else:
            result = worker_tool.run(
                task=task_prompt,
                cwd=cwd,
                mode="edit",
                timeout=stage_cfg.timeout,
                model=stage_cfg.model,
            )
        after = snapshot_workspace(Path(cwd))
        detected = diff_workspace_snapshots(before, after)
        changed_files = sorted(set(result.changed_files) | set(detected))
        created_files = sorted(set(after) - set(before))
        self._record_task_execution(
            task=task,
            stage_cfg=stage_cfg,
            cwd=cwd,
            result=result,
            changed_files=changed_files,
            crew_rounds=crew_rounds,
            parallel_batch_id=parallel_batch_id,
        )
        return result, changed_files, created_files

    def _deny_patterns(self) -> list[str]:
        return list(self.config.paths.get("deny") or [])

    def _denied_failure_result(
        self, denied: dict[str, str], unrestorable: list[str]
    ) -> CoderResult:
        details = ", ".join(
            f"{path} (matched {pattern!r})" for path, pattern in sorted(denied.items())
        )
        error = f"Denied paths touched: {details}."
        if unrestorable:
            error += " Could not restore: " + ", ".join(sorted(unrestorable)) + "."
        return CoderResult(summary="", raw_output="", exit_code=1, error=error)

    def _run_serial_task(
        self,
        *,
        worker_tool: HeadlessCoderTool,
        task: TaskItem,
        stage_cfg,
        plan_output: str,
        human_instructions: str | None,
    ) -> tuple[CoderResult, list[str]]:
        """Run one structured task serially, enforcing paths.deny.

        ``do_work.isolation: copy`` runs the task in a disposable workspace
        copy and merges only clean results back — denied or unsafe paths
        never reach the real repo, and a failed task leaves it pristine.
        The default ``in_place`` keeps today's behavior with post-hoc
        restore of denied paths.
        """
        deny = self._deny_patterns()
        isolation = stage_cfg.extra.get("isolation", "in_place")

        if isolation != "copy":
            result, changed_files, created_files = self._run_task_with_change_tracking(
                worker_tool=worker_tool,
                task=task,
                cwd=self.state.target_repo,
                stage_cfg=stage_cfg,
                plan_output=plan_output,
                human_instructions=human_instructions,
                parallel_batch_id=None,
            )
            denied = match_denied(changed_files, deny)
            if not denied:
                return result, changed_files
            unrestorable = restore_denied_paths(
                self.state.target_repo,
                sorted(denied),
                created=created_files,
            )
            remaining = [
                path
                for path in changed_files
                if path not in denied or path in unrestorable
            ]
            return self._denied_failure_result(denied, unrestorable), remaining

        try:
            workspace = create_workspace_copy(
                Path(self.state.target_repo),
                prefix=f"flow-serial-task-{task.id}-",
            )
        except OSError as exc:
            # A copy failure (disk full, permissions) fails this task, not
            # the whole run; the target repo is untouched.
            return (
                CoderResult(
                    summary="",
                    raw_output="",
                    exit_code=1,
                    error=f"Could not create isolated workspace copy: {exc}",
                ),
                [],
            )
        try:
            result, changed_files, _created = self._run_task_with_change_tracking(
                worker_tool=worker_tool,
                task=task,
                cwd=str(workspace),
                stage_cfg=stage_cfg,
                plan_output=plan_output,
                human_instructions=human_instructions,
                parallel_batch_id=None,
            )
            if not result.success:
                # Nothing merged: the target repo is untouched.
                return result, []
            denied = match_denied(changed_files, deny)
            if denied:
                return self._denied_failure_result(denied, []), []
            try:
                apply_changed_files(
                    src_root=workspace,
                    dest_root=Path(self.state.target_repo),
                    changed_files=changed_files,
                )
            except ValueError as exc:
                return (
                    CoderResult(
                        summary="",
                        raw_output="",
                        exit_code=1,
                        error=f"Mergeback rejected unsafe path: {exc}",
                    ),
                    [],
                )
            return result, changed_files
        finally:
            cleanup_workspace_copy(workspace)

    def _run_unstructured_edit(
        self,
        worker_tool: HeadlessCoderTool,
        stage_cfg,
        prompt: str,
    ) -> tuple[CoderResult, list[str]]:
        """Run the direct (task-less) edit, enforcing paths.deny.

        Snapshot-brackets the run so change detection no longer relies on
        the worker's self-report (mirrors finalize). Honors
        ``do_work.isolation: copy`` like the structured serial path.
        """
        deny = self._deny_patterns()
        isolation = stage_cfg.extra.get("isolation", "in_place")
        target = Path(self.state.target_repo)

        if isolation == "copy":
            try:
                workspace = create_workspace_copy(target, prefix="flow-direct-edit-")
            except OSError as exc:
                error = f"Could not create isolated workspace copy: {exc}"
                self.state.errors.append(error)
                return (
                    CoderResult(summary="", raw_output="", exit_code=1, error=error),
                    [],
                )
            try:
                before = snapshot_workspace(workspace)
                result = worker_tool.run(
                    task=prompt,
                    cwd=str(workspace),
                    mode="edit",
                    timeout=stage_cfg.timeout,
                    model=stage_cfg.model,
                )
                after = snapshot_workspace(workspace)
                changed = sorted(
                    set(result.changed_files)
                    | set(diff_workspace_snapshots(before, after))
                )
                if not result.success:
                    # Nothing merged: the target repo is untouched.
                    return result, []
                denied = match_denied(changed, deny)
                if denied:
                    failure = self._denied_failure_result(denied, [])
                    self.state.errors.append(failure.error or "")
                    return failure, []
                try:
                    apply_changed_files(
                        src_root=workspace,
                        dest_root=target,
                        changed_files=changed,
                    )
                except ValueError as exc:
                    error = f"Mergeback rejected unsafe path: {exc}"
                    self.state.errors.append(error)
                    return (
                        CoderResult(
                            summary="", raw_output="", exit_code=1, error=error
                        ),
                        [],
                    )
                return result, changed
            finally:
                cleanup_workspace_copy(workspace)

        before = snapshot_workspace(target)
        result = worker_tool.run(
            task=prompt,
            cwd=self.state.target_repo,
            mode="edit",
            timeout=stage_cfg.timeout,
            model=stage_cfg.model,
        )
        after = snapshot_workspace(target)
        changed = sorted(
            set(result.changed_files) | set(diff_workspace_snapshots(before, after))
        )
        denied = match_denied(changed, deny)
        if not denied:
            return result, changed
        created = sorted(set(after) - set(before))
        unrestorable = restore_denied_paths(target, sorted(denied), created=created)
        failure = self._denied_failure_result(denied, unrestorable)
        self.state.errors.append(failure.error or "")
        remaining = [
            path for path in changed if path not in denied or path in unrestorable
        ]
        return failure, remaining

    def _mark_task_complete(
        self,
        task: TaskItem,
        *,
        summary: str,
        changed_files: list[str],
    ) -> None:
        task.status = "done"
        task.review_notes = []
        task.last_error = None
        self._track_changed_files(changed_files)
        self._record_history(
            kind="task_complete",
            summary=f"Task {task.id} completed.",
            task_ids=[task.id],
            files=changed_files or task.files,
            details=[summary],
        )

    def _mark_task_failed(
        self,
        task: TaskItem,
        *,
        error: str,
        changed_files: list[str],
    ) -> None:
        task.status = "failed"
        task.last_error = error
        self._record_history(
            kind="task_failed",
            summary=f"Task {task.id} failed.",
            task_ids=[task.id],
            files=changed_files or task.files,
            details=[error],
        )

    def _parallel_conflicts(
        self, outcomes: list[ParallelTaskOutcome]
    ) -> dict[int, list[str]]:
        path_to_tasks: dict[str, set[int]] = {}
        for outcome in outcomes:
            if not outcome.success:
                continue
            for path in outcome.changed_files:
                path_to_tasks.setdefault(path, set()).add(outcome.task.id)

        conflicts: dict[int, set[str]] = {}
        for path, task_ids in path_to_tasks.items():
            if len(task_ids) < 2:
                continue
            for task_id in task_ids:
                conflicts.setdefault(task_id, set()).add(path)

        return {task_id: sorted(paths) for task_id, paths in conflicts.items()}

    def _parallel_batch_planner_enabled(self, stage_cfg) -> bool:
        parallel_cfg = stage_cfg.extra.get("parallel", {}) or {}
        planner_cfg = parallel_cfg.get("planner", {}) or {}
        return self._parallel_do_work_enabled(stage_cfg) and bool(
            planner_cfg.get("enabled", False)
        )

    def _revision_replanner_enabled(self) -> bool:
        do_work_cfg = self.config.get_stage("do_work")
        replan_cfg = do_work_cfg.extra.get("replan", {}) or {}
        return bool(replan_cfg.get("enabled", False))

    def _execution_replanner_enabled(self) -> bool:
        do_work_cfg = self.config.get_stage("do_work")
        replan_cfg = do_work_cfg.extra.get("replan", {}) or {}
        return bool(replan_cfg.get("enabled", False)) and bool(
            replan_cfg.get("on_execution_failure", False)
        )

    def _cross_task_success_replanner_enabled(self) -> bool:
        do_work_cfg = self.config.get_stage("do_work")
        replan_cfg = do_work_cfg.extra.get("replan", {}) or {}
        return bool(replan_cfg.get("enabled", False)) and bool(
            replan_cfg.get("on_cross_task_change", False)
        )

    def _ambiguous_success_replanner_enabled(self) -> bool:
        do_work_cfg = self.config.get_stage("do_work")
        replan_cfg = do_work_cfg.extra.get("replan", {}) or {}
        return bool(replan_cfg.get("enabled", False)) and bool(
            replan_cfg.get("on_ambiguous_success", False)
        )

    def _max_execution_replans(self) -> int:
        do_work_cfg = self.config.get_stage("do_work")
        replan_cfg = do_work_cfg.extra.get("replan", {}) or {}
        value = int(replan_cfg.get("max_execution_replans", 1))
        if value < 1:
            raise ValueError("do_work.replan.max_execution_replans must be at least 1")
        return value

    def _execution_replanning_count(self) -> int:
        return sum(
            1
            for entry in self._normalized_history_entries()
            if entry.kind == "execution_replanning"
        )

    def _planning_tool_for_do_work(
        self, worker_tool: HeadlessCoderTool
    ) -> HeadlessCoderTool:
        planning_skill = self.config.skills.get("plan")
        base_worker = getattr(worker_tool, "worker", worker_tool)
        loader = getattr(worker_tool, "loader", self.loader)
        return HeadlessCoderTool(
            worker=base_worker,
            skill_name=planning_skill,
            loader=loader,
        )

    def _build_batch_planning_prompt(
        self,
        *,
        ready_tasks: list[TaskItem],
        max_workers: int,
        plan_output: str,
        human_instructions: str | None = None,
    ) -> str:
        task_lines: list[str] = []
        for task in ready_tasks:
            files = ", ".join(task.files) if task.files else "unknown"
            notes = "; ".join(task.review_notes) if task.review_notes else "none"
            task_lines.append(
                f"- Task {task.id}: {task.title or task.description} | "
                f"files={files} | review_notes={notes}"
            )
        human_guidance = (
            f"\nHuman approval instructions:\n- {human_instructions}\n"
            if human_instructions
            else ""
        )
        return f"""You are selecting the next execution batch for structured do_work.

Original request:
{self.state.request}

Plan/spec context:
{plan_output[:3000]}

Ready tasks:
{chr(10).join(task_lines)}
{human_guidance}

Goal:
- Choose up to {max_workers} ready tasks for next execution batch.
- Prefer the largest batch likely safe to run in parallel in isolated workspaces.
- If uncertainty is high, return one task only.
- You may add likely file hints for selected tasks when current file lists look missing or weak.
- Never select tasks outside ready list.

Return only structured JSON matching schema.
""".strip()

    def _collect_batch_file_hints(
        self,
        *,
        ready_by_id: dict[int, TaskItem],
        planned_tasks,
    ) -> dict[int, list[str]]:
        hinted_files: dict[int, list[str]] = {}
        for planned_task in planned_tasks:
            task = ready_by_id.get(planned_task.task_id)
            if task is None:
                continue
            merged_files = list(task.files)
            existing = {path.strip() for path in task.files if path.strip()}
            for path in planned_task.files:
                normalized = path.strip()
                if normalized and normalized not in existing:
                    merged_files.append(normalized)
                    existing.add(normalized)
            hinted_files[task.id] = merged_files
        return hinted_files

    def _planned_batch_preview(
        self,
        *,
        ready_by_id: dict[int, TaskItem],
        task_ids: list[int],
        hinted_files: dict[int, list[str]],
    ) -> list[TaskItem]:
        preview: list[TaskItem] = []
        for task_id in task_ids:
            task = ready_by_id[task_id].model_copy(deep=True)
            task.files = list(hinted_files.get(task_id, task.files))
            preview.append(task)
        return preview

    def _ensure_conservative_planned_batch(self, batch: list[TaskItem]) -> None:
        if len(batch) < 2:
            return

        seen_files: set[str] = set()
        for task in batch:
            task_files = {path.strip() for path in task.files if path.strip()}
            if not task_files:
                raise ValueError(
                    "do_work batch planner returned a weak plan: "
                    f"task {task.id} still has no file hints"
                )
            overlap = task_files & seen_files
            if overlap:
                overlap_text = ", ".join(sorted(overlap))
                raise ValueError(
                    "do_work batch planner returned a weak plan: "
                    f"selected tasks still overlap on {overlap_text}"
                )
            seen_files |= task_files

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
        allowed_task_ids = set(execution_target_task_ids or []) or None
        ready = ready_execution_tasks(
            self.state.tasks,
            allowed_task_ids=allowed_task_ids,
        )
        if len(ready) < 2:
            return PlannedBatchSelection(
                task_ids=[],
                hinted_files={},
                summary="",
                planned_files=[],
            )

        planner_tool = self._planning_tool_for_do_work(worker_tool)
        prompt = self._build_batch_planning_prompt(
            ready_tasks=ready,
            max_workers=max_workers,
            plan_output=plan_output,
            human_instructions=human_instructions,
        )
        result = planner_tool.run(
            task=prompt,
            cwd=self.state.target_repo,
            mode="inspect",
            schema=DO_WORK_BATCH_PLAN_SCHEMA,
            timeout=stage_cfg.timeout,
            model=stage_cfg.model,
        )
        plan = normalize_do_work_batch_plan([result.summary, result.raw_output])
        if plan is None:
            raise ValueError("do_work batch planner output could not be parsed")

        ready_by_id = {task.id: task for task in ready}
        allowed_task_ids = set(ready_by_id)
        plan = require_concrete_do_work_batch_plan(
            plan,
            allowed_task_ids=allowed_task_ids,
            max_workers=max_workers,
        )
        task_ids = [planned_task.task_id for planned_task in plan.tasks]
        hinted_files = self._collect_batch_file_hints(
            ready_by_id=ready_by_id,
            planned_tasks=plan.tasks,
        )
        self._ensure_conservative_planned_batch(
            self._planned_batch_preview(
                ready_by_id=ready_by_id,
                task_ids=task_ids,
                hinted_files=hinted_files,
            )
        )
        return PlannedBatchSelection(
            task_ids=task_ids,
            hinted_files=hinted_files,
            summary=plan.summary,
            planned_files=sorted(
                {
                    path
                    for planned_task in plan.tasks
                    for path in planned_task.files
                    if path.strip()
                }
            ),
        )

    def _revision_planning_worker_and_stage(
        self, do_work_worker: HeadlessCoderTool
    ) -> tuple[HeadlessCoderTool, Any]:
        if "plan" in self.config.skills and "plan" in self._workers:
            return self._get_worker("plan"), self.config.get_stage("plan")
        return (
            self._planning_tool_for_do_work(do_work_worker),
            self.config.get_stage("do_work"),
        )

    def _build_revision_replan_prompt(
        self,
        *,
        target_ids: set[int],
        downstream_ids: set[int],
    ) -> str:
        current_plan = render_plan_markdown(
            state_items_to_plan_output(self.state.spec, self.state.tasks)
        )
        task_state_lines: list[str] = []
        for task in self.state.tasks:
            files = ", ".join(task.files) if task.files else "n/a"
            review_notes = "; ".join(task.review_notes) if task.review_notes else "none"
            dependencies = ", ".join(str(dep) for dep in task.dependencies) or "none"
            task_state_lines.append(
                f"- Task {task.id}: status={task.status} | deps={dependencies} | "
                f"files={files} | notes={review_notes}"
            )
        hint_lines: list[str] = []
        for hint in self._normalized_review_task_hints():
            hint_lines.append(
                f"- tasks={','.join(str(task_id) for task_id in hint.task_ids) or 'none'} "
                f"| files={','.join(hint.files) or 'none'} | {hint.summary}"
            )
        issues_text = "\n".join(f"- {issue}" for issue in self.state.issues) or "- None"
        human_replan_reason = self.state.pending_revision_replan_reason
        human_replan_text = (
            f"\nHuman-requested replanning guidance:\n- {human_replan_reason}\n"
            if human_replan_reason
            else ""
        )
        return f"""You are replanning the structured task graph after a failed review round.

Original request:
{self.state.request}

Current spec and task graph:
{current_plan}

Current execution state:
{chr(10).join(task_state_lines)}

Review issues:
{issues_text}

Review task hints:
{chr(10).join(hint_lines) or "- None"}
{human_replan_text}

Directly targeted task ids: {sorted(target_ids)}
Downstream task ids that may need rework: {sorted(downstream_ids)}

Recent history:
{self._history_summary(limit=8)}

Rules:
- Return a full revised structured plan object, not a partial patch.
- Preserve unchanged valid completed tasks with same ids whenever practical.
- You may split, merge, reorder, or add tasks when review evidence suggests current graph is wrong.
- Keep ids stable for unchanged tasks when possible.
- Update dependencies, files, acceptance criteria, and verification so remaining work is executable.
- Keep scope limited to satisfying current review issues.
""".strip()

    def _build_execution_replan_prompt(
        self,
        *,
        failed_task: TaskItem,
        error: str,
        changed_files: list[str],
        target_ids: set[int],
        downstream_ids: set[int],
    ) -> str:
        current_plan = render_plan_markdown(
            state_items_to_plan_output(self.state.spec, self.state.tasks)
        )
        task_state_lines: list[str] = []
        for task in self.state.tasks:
            files = ", ".join(task.files) if task.files else "n/a"
            review_notes = "; ".join(task.review_notes) if task.review_notes else "none"
            dependencies = ", ".join(str(dep) for dep in task.dependencies) or "none"
            task_state_lines.append(
                f"- Task {task.id}: status={task.status} | deps={dependencies} | "
                f"files={files} | notes={review_notes}"
            )
        changed = ", ".join(changed_files) if changed_files else "none"
        return f"""You are replanning the structured task graph after a task failed during do_work.

Original request:
{self.state.request}

Current spec and task graph:
{current_plan}

Current execution state:
{chr(10).join(task_state_lines)}

Runtime failure:
- Task id: {failed_task.id}
- Task title: {failed_task.title or failed_task.description}
- Error: {error}
- Changed files observed during failed attempt: {changed}

Directly targeted task ids: {sorted(target_ids)}
Downstream task ids that may need rework: {sorted(downstream_ids)}

Recent history:
{self._history_summary(limit=8)}

Rules:
- Return a full revised structured plan object, not a partial patch.
- Preserve unchanged valid completed tasks with same ids whenever practical.
- You may split, merge, reorder, or add remaining tasks to recover from this failure.
- Keep ids stable for unchanged tasks when possible.
- Update dependencies, files, acceptance criteria, and verification so remaining work is executable.
- Focus on recovering from runtime failure inside current do_work round.
""".strip()

    def _cross_task_success_targets(
        self,
        *,
        source_task: TaskItem,
        changed_files: list[str],
    ) -> tuple[set[int], list[str]]:
        changed = {path.strip() for path in changed_files if path.strip()}
        if not changed:
            return set(), []

        target_ids: set[int] = set()
        overlapping_files: set[str] = set()

        for task in self.state.tasks:
            if task.id == source_task.id or task.status == "done":
                continue
            task_files = {path.strip() for path in task.files if path.strip()}
            overlap = changed & task_files
            if overlap:
                target_ids.add(task.id)
                overlapping_files |= overlap

        return target_ids, sorted(overlapping_files)

    def _build_cross_task_success_replan_prompt(
        self,
        *,
        source_task: TaskItem,
        changed_files: list[str],
        target_ids: set[int],
        overlapping_files: list[str],
        downstream_ids: set[int],
    ) -> str:
        current_plan = render_plan_markdown(
            state_items_to_plan_output(self.state.spec, self.state.tasks)
        )
        changed = {path.strip() for path in changed_files if path.strip()}
        task_state_lines: list[str] = []
        impacted_lines: list[str] = []

        for task in self.state.tasks:
            files = ", ".join(task.files) if task.files else "n/a"
            review_notes = "; ".join(task.review_notes) if task.review_notes else "none"
            dependencies = ", ".join(str(dep) for dep in task.dependencies) or "none"
            task_state_lines.append(
                f"- Task {task.id}: status={task.status} | deps={dependencies} | "
                f"files={files} | notes={review_notes}"
            )
            if task.id in target_ids:
                overlap = sorted(
                    {path.strip() for path in task.files if path.strip()} & changed
                )
                impacted_lines.append(
                    f"- Task {task.id}: status={task.status} | overlap={', '.join(overlap) or 'none'} "
                    f"| files={files}"
                )

        return f"""You are replanning the structured task graph after a successful do_work task changed files assigned to other remaining tasks.

Original request:
{self.state.request}

Current spec and task graph:
{current_plan}

Current execution state:
{chr(10).join(task_state_lines)}

Successful task evidence:
- Task id: {source_task.id}
- Task title: {source_task.title or source_task.description}
- Changed files observed: {", ".join(changed_files) or "none"}
- Overlapping planned files for remaining tasks: {", ".join(overlapping_files) or "none"}

Impacted remaining tasks:
{chr(10).join(impacted_lines) or "- None"}

Directly targeted task ids: {sorted(target_ids)}
Downstream task ids that may need rework: {sorted(downstream_ids)}

Recent history:
{self._history_summary(limit=8)}

Rules:
- Return a full revised structured plan object, not a partial patch.
- Preserve unchanged valid completed tasks with same ids whenever practical.
- You may split, merge, reorder, or add remaining tasks when this success evidence shows current task boundaries are stale.
- Keep ids stable for unchanged tasks when possible.
- Update dependencies, files, acceptance criteria, and verification so remaining work is executable.
- Focus only on work made stale or newly clarified by the observed cross-task file changes.
""".strip()

    def _ambiguous_success_reasons(
        self, *, task: TaskItem, changed_files: list[str]
    ) -> list[str]:
        changed = {path.strip() for path in changed_files if path.strip()}
        planned = {path.strip() for path in task.files if path.strip()}
        reasons: list[str] = []

        if not changed:
            reasons.append("Task exited successfully but no files changed.")
        if changed and planned and not (changed & planned):
            reasons.append(
                "Task exited successfully but none of its planned files were changed."
            )

        return reasons

    def _build_ambiguous_success_replan_prompt(
        self,
        *,
        source_task: TaskItem,
        changed_files: list[str],
        reasons: list[str],
        target_ids: set[int],
        downstream_ids: set[int],
    ) -> str:
        current_plan = render_plan_markdown(
            state_items_to_plan_output(self.state.spec, self.state.tasks)
        )
        task_state_lines: list[str] = []
        for task in self.state.tasks:
            files = ", ".join(task.files) if task.files else "n/a"
            review_notes = "; ".join(task.review_notes) if task.review_notes else "none"
            dependencies = ", ".join(str(dep) for dep in task.dependencies) or "none"
            task_state_lines.append(
                f"- Task {task.id}: status={task.status} | deps={dependencies} | "
                f"files={files} | notes={review_notes}"
            )

        planned = ", ".join(source_task.files) if source_task.files else "none"
        changed = ", ".join(changed_files) if changed_files else "none"
        reason_lines = "\n".join(f"- {reason}" for reason in reasons) or "- None"
        return f"""You are replanning the structured task graph after a do_work task reported success with ambiguous execution evidence.

Original request:
{self.state.request}

Current spec and task graph:
{current_plan}

Current execution state:
{chr(10).join(task_state_lines)}

Ambiguous success evidence:
- Task id: {source_task.id}
- Task title: {source_task.title or source_task.description}
- Planned task files: {planned}
- Changed files observed: {changed}
- Evidence concerns:
{reason_lines}

Directly targeted task ids: {sorted(target_ids)}
Downstream task ids that may need rework: {sorted(downstream_ids)}

Recent history:
{self._history_summary(limit=8)}

Rules:
- Return a full revised structured plan object, not a partial patch.
- Preserve unchanged valid completed tasks with same ids whenever practical.
- You may split, merge, reorder, or add remaining tasks when this weak success evidence suggests the current graph is stale or unfinished.
- Keep ids stable for unchanged tasks when possible.
- Update dependencies, files, acceptance criteria, and verification so remaining work is executable.
- Focus on recovering from the ambiguous success evidence inside the current do_work round.
""".strip()

    def _task_definition_matches(self, current: TaskItem, replanned: TaskItem) -> bool:
        return (
            current.title == replanned.title
            and current.description == replanned.description
            and current.acceptance_criteria == replanned.acceptance_criteria
            and current.verification == replanned.verification
            and current.dependencies == replanned.dependencies
            and current.files == replanned.files
            and current.estimated_scope == replanned.estimated_scope
        )

    def _apply_replanned_tasks(
        self,
        *,
        plan: PlanOutput,
        target_ids: set[int],
        downstream_ids: set[int],
    ) -> None:
        existing_by_id = {task.id: task for task in self.state.tasks}
        notes_by_task = self._build_revision_notes_by_task(target_ids)
        reset_ids = target_ids | downstream_ids
        replanned_tasks = plan_tasks_to_state_items(plan.tasks)

        for task in replanned_tasks:
            previous = existing_by_id.get(task.id)
            if (
                previous is not None
                and task.id not in reset_ids
                and self._task_definition_matches(previous, task)
            ):
                task.status = previous.status
                task.review_notes = previous.review_notes.copy()
                task.last_error = previous.last_error
                continue

            if task.id in target_ids:
                task.status = "needs_revision"
                task.review_notes = notes_by_task.get(task.id, self.state.issues.copy())
                task.last_error = None
                continue

            if task.id in downstream_ids:
                task.status = "pending"
                task.review_notes = ["Upstream dependency requires revision."]
                task.last_error = None
                continue

            if (
                previous is not None
                and previous.status != "done"
                and self._task_definition_matches(previous, task)
            ):
                task.status = previous.status
                task.review_notes = previous.review_notes.copy()
                task.last_error = previous.last_error
            else:
                task.status = "pending"
                task.review_notes = []
                task.last_error = None

        self.state.spec = plan.spec
        self.state.tasks = replanned_tasks
        self.state.review_task_hints = []

    def _attempt_structured_revision_replan(
        self, do_work_worker: HeadlessCoderTool
    ) -> bool:
        human_requested = self._revision_replan_requested()
        if (not self._revision_replanner_enabled() and not human_requested) or (
            not self.state.tasks
        ):
            return False

        target_ids = self._collect_target_task_ids()
        downstream_ids = self._expand_dependent_task_ids(target_ids)
        planning_worker, stage_cfg = self._revision_planning_worker_and_stage(
            do_work_worker
        )
        prompt = self._build_revision_replan_prompt(
            target_ids=target_ids,
            downstream_ids=downstream_ids,
        )
        try:
            result = planning_worker.run(
                task=prompt,
                cwd=self.state.target_repo,
                mode="inspect",
                schema=PlanOutput.model_json_schema(),
                timeout=stage_cfg.timeout,
                model=stage_cfg.model,
            )
            plan = require_concrete_plan(
                normalize_plan_output([result.raw_output, result.summary])
            )
        except Exception:
            self._clear_pending_revision_replan()
            return False

        human_reason = self.state.pending_revision_replan_reason
        self._clear_pending_revision_replan()
        self._apply_replanned_tasks(
            plan=plan,
            target_ids=target_ids,
            downstream_ids=downstream_ids,
        )
        self._record_history(
            kind="revision_replanning",
            summary=(
                "Replanned structured task graph from human review request."
                if human_requested
                else "Replanned structured task graph."
            ),
            task_ids=[task.id for task in self.state.tasks if task.status != "done"],
            files=sorted(
                {
                    path
                    for task in self.state.tasks
                    for path in task.files
                    if path.strip()
                }
            ),
            details=[
                *([human_reason] if human_requested and human_reason else []),
                plan.spec,
            ],
        )
        return True

    def _attempt_structured_execution_replan(
        self,
        *,
        do_work_worker: HeadlessCoderTool,
        failed_task: TaskItem,
        error: str,
        changed_files: list[str],
    ) -> bool:
        if (
            not self._execution_replanner_enabled()
            or not self.state.tasks
            or self._execution_replanning_count() >= self._max_execution_replans()
        ):
            return False

        target_ids = {failed_task.id}
        downstream_ids = self._expand_dependent_task_ids(target_ids)
        planning_worker, stage_cfg = self._revision_planning_worker_and_stage(
            do_work_worker
        )
        prompt = self._build_execution_replan_prompt(
            failed_task=failed_task,
            error=error,
            changed_files=changed_files,
            target_ids=target_ids,
            downstream_ids=downstream_ids,
        )
        try:
            result = planning_worker.run(
                task=prompt,
                cwd=self.state.target_repo,
                mode="inspect",
                schema=PlanOutput.model_json_schema(),
                timeout=stage_cfg.timeout,
                model=stage_cfg.model,
            )
            plan = require_concrete_plan(
                normalize_plan_output([result.raw_output, result.summary])
            )
        except Exception:
            return False

        self._apply_replanned_tasks(
            plan=plan,
            target_ids=target_ids,
            downstream_ids=downstream_ids,
        )
        self._record_history(
            kind="execution_replanning",
            summary=(
                f"Replanned structured task graph after task {failed_task.id} "
                "failed during do_work."
            ),
            task_ids=[task.id for task in self.state.tasks if task.status != "done"],
            files=sorted(
                {
                    path
                    for task in self.state.tasks
                    for path in task.files
                    if path.strip()
                }
            ),
            details=[error, plan.spec],
        )
        return True

    def _attempt_structured_cross_task_success_replan(
        self,
        *,
        do_work_worker: HeadlessCoderTool,
        source_task: TaskItem,
        changed_files: list[str],
    ) -> bool:
        if (
            not self._cross_task_success_replanner_enabled()
            or not self.state.tasks
            or self._execution_replanning_count() >= self._max_execution_replans()
        ):
            return False

        target_ids, overlapping_files = self._cross_task_success_targets(
            source_task=source_task,
            changed_files=changed_files,
        )
        if not target_ids:
            return False

        downstream_ids = self._expand_dependent_task_ids(target_ids)
        planning_worker, stage_cfg = self._revision_planning_worker_and_stage(
            do_work_worker
        )
        prompt = self._build_cross_task_success_replan_prompt(
            source_task=source_task,
            changed_files=changed_files,
            target_ids=target_ids,
            overlapping_files=overlapping_files,
            downstream_ids=downstream_ids,
        )
        try:
            result = planning_worker.run(
                task=prompt,
                cwd=self.state.target_repo,
                mode="inspect",
                schema=PlanOutput.model_json_schema(),
                timeout=stage_cfg.timeout,
                model=stage_cfg.model,
            )
            plan = require_concrete_plan(
                normalize_plan_output([result.raw_output, result.summary])
            )
        except Exception:
            return False

        self._apply_replanned_tasks(
            plan=plan,
            target_ids=target_ids,
            downstream_ids=downstream_ids,
        )
        self._record_history(
            kind="execution_replanning",
            summary=(
                f"Replanned structured task graph after task {source_task.id} "
                "changed files assigned to other tasks during do_work."
            ),
            task_ids=[task.id for task in self.state.tasks if task.status != "done"],
            files=sorted(
                {
                    path
                    for task in self.state.tasks
                    for path in task.files
                    if path.strip()
                }
            ),
            details=[
                f"Changed files: {', '.join(changed_files) or 'none'}",
                f"Overlapping planned files: {', '.join(overlapping_files) or 'none'}",
                plan.spec,
            ],
        )
        return True

    def _attempt_structured_ambiguous_success_replan(
        self,
        *,
        do_work_worker: HeadlessCoderTool,
        source_task: TaskItem,
        changed_files: list[str],
    ) -> bool:
        if (
            not self._ambiguous_success_replanner_enabled()
            or not self.state.tasks
            or self._execution_replanning_count() >= self._max_execution_replans()
        ):
            return False

        reasons = self._ambiguous_success_reasons(
            task=source_task,
            changed_files=changed_files,
        )
        if not reasons:
            return False

        target_ids = {source_task.id}
        downstream_ids = self._expand_dependent_task_ids(target_ids)
        planning_worker, stage_cfg = self._revision_planning_worker_and_stage(
            do_work_worker
        )
        prompt = self._build_ambiguous_success_replan_prompt(
            source_task=source_task,
            changed_files=changed_files,
            reasons=reasons,
            target_ids=target_ids,
            downstream_ids=downstream_ids,
        )
        try:
            result = planning_worker.run(
                task=prompt,
                cwd=self.state.target_repo,
                mode="inspect",
                schema=PlanOutput.model_json_schema(),
                timeout=stage_cfg.timeout,
                model=stage_cfg.model,
            )
            plan = require_concrete_plan(
                normalize_plan_output([result.raw_output, result.summary])
            )
        except Exception:
            return False

        self._apply_replanned_tasks(
            plan=plan,
            target_ids=target_ids,
            downstream_ids=downstream_ids,
        )
        self._record_history(
            kind="execution_replanning",
            summary=(
                f"Replanned structured task graph after task {source_task.id} "
                "reported ambiguous success during do_work."
            ),
            task_ids=[task.id for task in self.state.tasks if task.status != "done"],
            files=sorted(
                {
                    path
                    for task in self.state.tasks
                    for path in task.files
                    if path.strip()
                }
            ),
            details=[
                *reasons,
                f"Changed files: {', '.join(changed_files) or 'none'}",
                plan.spec,
            ],
        )
        return True

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
        allowed_task_ids = set(execution_target_task_ids or []) or None
        static_batch = select_execution_batch(
            self.state.tasks,
            max_workers,
            allowed_task_ids=allowed_task_ids,
        )
        if not self._parallel_batch_planner_enabled(stage_cfg):
            return static_batch

        ready = ready_execution_tasks(
            self.state.tasks,
            allowed_task_ids=allowed_task_ids,
        )
        if len(ready) <= len(static_batch) or len(static_batch) >= max(1, max_workers):
            return static_batch

        ready_by_id = {task.id: task for task in ready}
        try:
            planned_selection = self._planned_execution_batch(
                worker_tool=worker_tool,
                stage_cfg=stage_cfg,
                plan_output=plan_output,
                max_workers=max_workers,
                human_instructions=human_instructions,
                execution_target_task_ids=execution_target_task_ids,
            )
        except Exception:
            return static_batch

        if len(planned_selection.task_ids) <= len(static_batch):
            return static_batch

        for task_id, files in planned_selection.hinted_files.items():
            ready_by_id[task_id].files = list(files)

        self._record_history(
            kind="batch_planning",
            summary="Prepared dynamic execution batch.",
            task_ids=list(planned_selection.task_ids),
            files=planned_selection.planned_files,
            details=[planned_selection.summary],
        )
        return [ready_by_id[task_id] for task_id in planned_selection.task_ids]

    def _run_structured_do_work(
        self,
        worker_tool: HeadlessCoderTool,
        stage_cfg,
        plan_output: str,
        human_instructions: str | None = None,
        execution_target_task_ids: list[int] | None = None,
    ) -> str:
        parallel_cfg = stage_cfg.extra.get("parallel", {}) or {}
        allowed_task_ids = set(execution_target_task_ids or []) or None
        max_workers = 1
        if self._parallel_do_work_enabled(stage_cfg):
            max_workers = int(parallel_cfg.get("max_workers", 2))
        task_summaries: list[str] = []
        failures: list[str] = []
        batch_counter = 0

        while has_pending_tasks(self.state.tasks, allowed_task_ids=allowed_task_ids):
            batch = self._select_structured_execution_batch(
                worker_tool=worker_tool,
                stage_cfg=stage_cfg,
                plan_output=plan_output,
                max_workers=max_workers,
                human_instructions=human_instructions,
                execution_target_task_ids=execution_target_task_ids,
            )
            if not batch:
                pending = [
                    task.id
                    for task in self.state.tasks
                    if task.status != "done"
                    and (allowed_task_ids is None or task.id in allowed_task_ids)
                ]
                failures.append(
                    f"No executable task batch found for pending tasks: {pending}"
                )
                break

            for task in batch:
                task.status = "in_progress"

            if len(batch) == 1:
                task = batch[0]
                result, changed_files = self._run_serial_task(
                    worker_tool=worker_tool,
                    task=task,
                    stage_cfg=stage_cfg,
                    plan_output=plan_output,
                    human_instructions=human_instructions,
                )
                if result.success:
                    self._mark_task_complete(
                        task,
                        summary=result.summary or result.raw_output,
                        changed_files=changed_files,
                    )
                    if self._attempt_structured_cross_task_success_replan(
                        do_work_worker=worker_tool,
                        source_task=task,
                        changed_files=changed_files,
                    ):
                        task_summaries.append(
                            f"Task {task.id} complete; replanned remaining work from cross-task changes."
                        )
                        continue
                    if self._attempt_structured_ambiguous_success_replan(
                        do_work_worker=worker_tool,
                        source_task=task,
                        changed_files=changed_files,
                    ):
                        task_summaries.append(
                            f"Task {task.id} complete; replanned remaining work from ambiguous success evidence."
                        )
                        continue
                    task_summaries.append(
                        f"Task {task.id} complete: {result.summary or result.raw_output}"
                    )
                else:
                    error = result.error or result.summary or result.raw_output
                    if self._attempt_structured_execution_replan(
                        do_work_worker=worker_tool,
                        failed_task=task,
                        error=error,
                        changed_files=changed_files,
                    ):
                        self._track_changed_files(changed_files)
                        task_summaries.append(
                            f"Task {task.id} failed; replanned remaining work."
                        )
                        continue
                    self._mark_task_failed(
                        task,
                        error=error,
                        changed_files=changed_files,
                    )
                    failures.append(f"Task {task.id} failed: {error}")
                    break
                continue

            target_repo = Path(self.state.target_repo)
            batch_counter += 1
            batch_id = f"b{batch_counter}"
            workspaces = {
                task.id: create_workspace_copy(
                    target_repo,
                    prefix=f"flow-parallel-task-{task.id}-",
                )
                for task in batch
            }
            outcomes: list[ParallelTaskOutcome] = []

            try:
                with ThreadPoolExecutor(max_workers=len(batch)) as pool:
                    future_map = {
                        pool.submit(
                            self._run_task_with_change_tracking,
                            worker_tool=worker_tool,
                            task=task,
                            cwd=str(workspaces[task.id]),
                            stage_cfg=stage_cfg,
                            plan_output=plan_output,
                            human_instructions=human_instructions,
                            parallel_batch_id=batch_id,
                        ): task
                        for task in batch
                    }
                    for future in as_completed(future_map):
                        task = future_map[future]
                        workspace = workspaces[task.id]
                        try:
                            result, changed_files, _created = future.result()
                        except Exception as exc:
                            outcomes.append(
                                ParallelTaskOutcome(
                                    task=task,
                                    success=False,
                                    summary="",
                                    changed_files=[],
                                    error=str(exc),
                                    workspace=str(workspace),
                                )
                            )
                            continue

                        outcomes.append(
                            ParallelTaskOutcome(
                                task=task,
                                success=result.success,
                                summary=result.summary or result.raw_output,
                                changed_files=changed_files,
                                error=result.error
                                or result.summary
                                or result.raw_output,
                                workspace=str(workspace),
                            )
                        )

                conflicts = self._parallel_conflicts(outcomes)
                ordered_outcomes = sorted(outcomes, key=lambda outcome: outcome.task.id)
                failed_outcomes: list[tuple[ParallelTaskOutcome, str]] = []
                success_replanned_task_id: int | None = None
                success_replanned_reason: str | None = None

                for outcome in ordered_outcomes:
                    workspace = Path(outcome.workspace or "")
                    if outcome.task.id in conflicts:
                        failed_outcomes.append(
                            (
                                outcome,
                                "Parallel batch changed overlapping files: "
                                + ", ".join(conflicts[outcome.task.id]),
                            )
                        )
                        continue

                    if not outcome.success:
                        failed_outcomes.append(
                            (
                                outcome,
                                outcome.error or "Task failed in parallel batch.",
                            )
                        )
                        continue

                    denied = match_denied(outcome.changed_files, self._deny_patterns())
                    if denied:
                        # Fail closed: merging an allowed subset would mark
                        # the task complete while silently dropping changes.
                        # Nothing leaves the isolated copy.
                        details = ", ".join(
                            f"{path} (matched {pattern!r})"
                            for path, pattern in sorted(denied.items())
                        )
                        failed_outcomes.append(
                            (outcome, f"Denied paths touched: {details}.")
                        )
                        continue

                    try:
                        apply_changed_files(
                            src_root=workspace,
                            dest_root=target_repo,
                            changed_files=outcome.changed_files,
                        )
                    except ValueError as exc:
                        failed_outcomes.append(
                            (outcome, f"Mergeback rejected unsafe path: {exc}")
                        )
                        continue
                    self._mark_task_complete(
                        outcome.task,
                        summary=outcome.summary,
                        changed_files=outcome.changed_files,
                    )
                    task_summaries.append(
                        f"Task {outcome.task.id} complete: {outcome.summary}"
                    )

                if failed_outcomes:
                    failed_outcome, failed_error = failed_outcomes[0]
                    if self._attempt_structured_execution_replan(
                        do_work_worker=worker_tool,
                        failed_task=failed_outcome.task,
                        error=failed_error,
                        changed_files=failed_outcome.changed_files,
                    ):
                        task_summaries.append(
                            f"Task {failed_outcome.task.id} failed; replanned remaining work."
                        )
                        continue

                    for outcome, error in failed_outcomes:
                        self._mark_task_failed(
                            outcome.task,
                            error=error,
                            changed_files=outcome.changed_files,
                        )
                        failures.append(f"Task {outcome.task.id} failed: {error}")
                else:
                    for outcome in ordered_outcomes:
                        if self._attempt_structured_cross_task_success_replan(
                            do_work_worker=worker_tool,
                            source_task=outcome.task,
                            changed_files=outcome.changed_files,
                        ):
                            success_replanned_task_id = outcome.task.id
                            success_replanned_reason = "cross-task changes"
                            break
                        if self._attempt_structured_ambiguous_success_replan(
                            do_work_worker=worker_tool,
                            source_task=outcome.task,
                            changed_files=outcome.changed_files,
                        ):
                            success_replanned_task_id = outcome.task.id
                            success_replanned_reason = "ambiguous success evidence"
                            break
            finally:
                for workspace in workspaces.values():
                    cleanup_workspace_copy(workspace)

            if success_replanned_task_id is not None:
                task_summaries.append(
                    f"Task {success_replanned_task_id} complete; replanned remaining work from {success_replanned_reason}."
                )
                continue

            if failures:
                break

        pending = [task.id for task in self.state.tasks if task.status != "done"]
        if (
            allowed_task_ids is not None
            and pending
            and not has_pending_tasks(
                self.state.tasks, allowed_task_ids=allowed_task_ids
            )
        ):
            remaining_untargeted = [
                task_id for task_id in pending if task_id not in allowed_task_ids
            ]
            if remaining_untargeted:
                task_summaries.append(
                    "Focused execution completed the targeted task set; "
                    f"remaining untargeted tasks: {remaining_untargeted}"
                )
        if pending:
            task_summaries.append(f"Pending tasks remaining: {pending}")
        if failures:
            self.state.errors.extend(failures)
            task_summaries.extend(failures)

        return "\n".join(task_summaries).strip() or "No structured tasks were executed."

    @listen("plan")
    def do_work(self, plan_output: str) -> str:
        if self._is_terminal_status():
            logger.info(
                f"[Flow] Skipping do_work because flow is terminal: {self.state.status}"
            )
            return self._terminal_result()
        self._mark_running()
        self._log_event("stage_start", stage="do_work")
        worker_tool = self._get_worker("do_work")
        stage_cfg = self.config.get_stage("do_work")
        human_gate_message = (
            "About to run the expensive edit stage (do_work). "
            "This will let the headless coder modify files."
        )
        execution_target_task_ids: list[int] | None = None

        while True:
            decision = self._maybe_ask_human("do_work", human_gate_message)
            if decision.proceed:
                break
            if decision.action == "skip-to-review":
                logger.info(
                    "[Flow] Human skipped do_work and routed directly to review."
                )
                self.state.last_stage = "do_work"
                self._refresh_debug_report()
                return (
                    "Human skipped do_work before edit stage. "
                    "No automated edits were performed in this stage."
                )
            if decision.action == "replan":
                logger.info("[Flow] Human requested replanning before do_work.")
                self._record_history(
                    kind="human_replanning",
                    summary="Human requested a fresh plan before do_work.",
                    details=[
                        decision.instructions
                        or "Human requested replanning before the edit stage."
                    ],
                )
                plan_output = self._execute_plan_stage(
                    human_instructions=decision.instructions,
                    current_plan_output=plan_output,
                    replanning_reason=(
                        "Human requested replanning before entering do_work."
                    ),
                )
                continue
            if decision.action == "target-tasks":
                selected_ids = decision.task_ids or []
                execution_target_task_ids = sorted(
                    self._expand_required_task_ids(set(selected_ids))
                )
                dependency_task_ids = [
                    task_id
                    for task_id in execution_target_task_ids
                    if task_id not in selected_ids
                ]
                reason = (
                    decision.instructions
                    or "Human selected tasks for focused execution before do_work."
                )
                self._record_history(
                    kind="execution_targeting",
                    summary="Human narrowed do_work to targeted tasks.",
                    task_ids=execution_target_task_ids,
                    files=self._task_files_for_ids(execution_target_task_ids),
                    details=[
                        "Requested tasks: "
                        + ", ".join(str(task_id) for task_id in selected_ids),
                        *(
                            [
                                "Auto-included dependency tasks: "
                                + ", ".join(
                                    str(task_id) for task_id in dependency_task_ids
                                )
                            ]
                            if dependency_task_ids
                            else []
                        ),
                        reason,
                    ],
                )
                logger.info("[Flow] Human narrowed do_work to targeted tasks.")
                break
            logger.info("[Flow] Human aborted before do_work.")
            self._mark_human_abort(
                "do_work",
                stage_input=plan_output,
                message=human_gate_message,
            )
            return "aborted-by-human"

        logger.info(
            f"\n[Flow] do_work using {stage_cfg.worker} (skill: {stage_cfg.skill})"
        )

        if self.state.tasks:
            self.state.last_stage = "do_work"
            return self._run_structured_do_work(
                worker_tool,
                stage_cfg,
                plan_output,
                human_instructions=decision.instructions,
                execution_target_task_ids=execution_target_task_ids,
            )

        human_guidance = (
            f"\nHuman approval instructions:\n- {decision.instructions}\n"
            if decision.instructions
            else ""
        )

        prompt = f"""Follow the assigned operating procedure for this implementation stage.

Plan / spec context:
{plan_output[:3000]}

Original user request:
{self.state.request}

Target repo: {self.state.target_repo}

Current revision count: {self.state.revisions}
{human_guidance}

Execute the work. After you are done, summarize what changed and whether tests now pass.
"""

        result, changed_files = self._run_unstructured_edit(
            worker_tool, stage_cfg, prompt
        )

        if changed_files:
            self.state.changed_files.extend(changed_files)

        self.state.last_stage = "do_work"
        return result.summary or result.raw_output or result.error or ""

    # ------------------------------------------------------------------
    # Review router - uses configured worker in INSPECT (read-only) mode
    # ------------------------------------------------------------------
    @router("do_work")
    def review(self, work_summary: str) -> Literal["pass", "revise", "aborted"]:
        if self._is_terminal_status():
            logger.info(
                f"[Flow] Skipping review because flow is terminal: {self.state.status}"
            )
            return "aborted"
        self._mark_running()
        self._log_event("stage_start", stage="review")
        self.state.latest_work_summary = work_summary
        stage_cfg = self.config.get_stage("review")

        logger.info(
            f"\n[Flow] review using {stage_cfg.worker} in INSPECT mode (skill: {stage_cfg.skill})"
        )
        before_review_message = (
            "About to run read-only review stage (review). "
            "This will inspect current changes and may trigger another revision loop."
        )
        human_decision = self._maybe_ask_human(
            "review",
            before_review_message,
        )
        if not human_decision.proceed:
            if human_decision.action == "force-revise":
                logger.info(
                    "[Flow] Human forced review revise without inspect-mode worker."
                )
                reason = (
                    human_decision.instructions
                    or "Human requested revision before inspect-mode review."
                )
                self.state.last_stage = "review"
                self.state.review_status = "revise"
                self.state.issues = [reason]
                self.state.review_task_hints = []
                self._refresh_debug_report()
                self._record_history(
                    kind="review_decision",
                    summary="Review forced to revise by human.",
                    details=[reason],
                )
                return "revise"
            if human_decision.action == "replan":
                logger.info(
                    "[Flow] Human requested replanning without inspect-mode review."
                )
                reason = self._set_pending_revision_replan(human_decision.instructions)
                self.state.last_stage = "review"
                self.state.review_status = "revise"
                self.state.issues = [reason]
                self.state.review_task_hints = []
                self._refresh_debug_report()
                self._record_history(
                    kind="human_replanning",
                    summary="Human requested replanning before automated review.",
                    details=[reason, "Skipped inspect-mode review worker."],
                )
                return "revise"
            if human_decision.action == "force-pass":
                logger.info(
                    "[Flow] Human forced review pass without inspect-mode worker."
                )
                self.state.last_stage = "review"
                self.state.review_status = "pass"
                self.state.issues = []
                self.state.review_task_hints = []
                self._refresh_debug_report()
                self._record_history(
                    kind="review_decision",
                    summary="Review forced to pass by human.",
                    details=["Skipped inspect-mode review worker."],
                )
                return "pass"
            if human_decision.action == "target-tasks":
                logger.info(
                    "[Flow] Human selected targeted revision tasks without inspect-mode worker."
                )
                selected_ids = human_decision.task_ids or []
                reason = (
                    human_decision.instructions
                    or "Human selected tasks for targeted revision before automated review."
                )
                self.state.last_stage = "review"
                self.state.review_status = "revise"
                self.state.issues = [reason]
                self.state.review_task_hints = [
                    ReviewTaskHint(
                        task_ids=selected_ids,
                        files=self._task_files_for_ids(selected_ids),
                        summary=reason,
                    )
                ]
                self._refresh_debug_report()
                self._record_history(
                    kind="revision_targeting",
                    summary="Human selected targeted revision tasks before automated review.",
                    task_ids=selected_ids,
                    files=self._task_files_for_ids(selected_ids),
                    details=["Skipped inspect-mode review worker.", reason],
                )
                return "revise"
            logger.info("[Flow] Human aborted before review.")
            self._mark_human_abort(
                "review",
                stage_input=work_summary,
                message=before_review_message,
            )
            return "aborted"

        human_guidance = human_decision.instructions
        review_rerun_guidance: str | None = None

        while True:
            decision = self._run_verified_review_once(
                work_summary=work_summary,
                human_guidance=human_guidance,
                review_rerun_guidance=review_rerun_guidance,
            )
            logger.info(
                f"[Flow] Review decision: {decision.status} | Issues: {len(decision.issues)}"
            )
            result, review_rerun_guidance = self._handle_after_review_checkpoint(
                work_summary=work_summary,
                message=self._after_review_message(decision),
                automated_status=decision.status,
                human_guidance=human_guidance,
            )
            if result == "rerun-review":
                continue
            return result

    # ------------------------------------------------------------------
    # Bounded revise loop
    # ------------------------------------------------------------------
    @listen("revise")
    def revise(self, decision: str) -> str:
        if self._is_terminal_status():
            logger.info(
                f"[Flow] Skipping revise because flow is terminal: {self.state.status}"
            )
            return self._terminal_result()
        self._mark_running()
        self._log_event("stage_start", stage="revise")
        self.state.increment_revision()
        logger.info(
            f"\n[Flow] Revising (revision {self.state.revisions}/{self.state.max_revisions})"
        )

        if self.state.revisions >= self.state.max_revisions:
            message = "Max revisions reached before review could pass."
            logger.warning("[Flow] Max revisions reached. Marking flow failed.")
            self.state.last_stage = "revise"
            self.state.status = "failed"
            self.state.review_status = "revise"
            self.state.issues = [*self.state.issues, message]
            self.state.errors.append(message)
            self._log_event("run_failed", reason=message)
            self._refresh_debug_report()
            self._record_history(
                kind="review_decision",
                summary="Flow failed after max revisions.",
                task_ids=sorted(
                    task.id for task in self.state.tasks if task.status != "done"
                ),
                details=self.state.issues[:3],
            )
            return "failed"

        if self.state.tasks:
            do_work_worker = self._get_worker("do_work")
            if self._attempt_structured_revision_replan(do_work_worker):
                active_ids = [
                    task.id for task in self.state.tasks if task.status != "done"
                ]
                return self._build_structured_revision_prompt(active_ids)
            return self._prepare_structured_revision_prompt()

        # Loop back to do_work with the issues as additional context
        issues_text = "\n".join(f"- {i}" for i in self.state.issues)
        return f"Previous review found the following issues that must be fixed:\n{issues_text}"

    def _planned_task_review_context(self) -> str:
        if not self.state.tasks:
            return "- No structured tasks available"

        lines = []
        for task in self.state.tasks:
            files = ", ".join(task.files) if task.files else "n/a"
            lines.append(
                f"- Task {task.id}: {task.title or task.description} | status={task.status} | files={files}"
            )
        return "\n".join(lines)

    def _review_target_summary(self) -> str:
        hints = self._normalized_review_task_hints()
        if not hints:
            return "- None"

        lines: list[str] = []
        for hint in hints:
            target_bits = []
            if hint.task_ids:
                target_bits.append(
                    f"tasks={','.join(str(task_id) for task_id in hint.task_ids)}"
                )
            if hint.files:
                target_bits.append(f"files={','.join(hint.files)}")
            target_text = " | ".join(target_bits) if target_bits else "unmapped"
            lines.append(f"- {target_text}: {hint.summary}")
        return "\n".join(lines)

    def _task_files_for_ids(self, task_ids: list[int]) -> list[str]:
        selected = []
        wanted = set(task_ids)
        for task in self.state.tasks:
            if task.id not in wanted:
                continue
            for path in task.files:
                if path not in selected:
                    selected.append(path)
        return selected

    def _task_ids_for_file_selector(self, raw_selector: str) -> list[int]:
        wanted = raw_selector.strip()
        if not wanted:
            return []

        selected: list[int] = []
        for task in self.state.tasks:
            task_files = [path.strip() for path in task.files if path.strip()]
            if wanted in task_files:
                selected.append(task.id)
        return selected

    def _hinted_task_ids(self) -> list[int]:
        task_map = {task.id: task for task in self.state.tasks}
        target_ids: set[int] = set()

        for hint in self._normalized_review_task_hints():
            for task_id in hint.task_ids:
                if task_id in task_map:
                    target_ids.add(task_id)
            if hint.files:
                hint_files = {path.strip() for path in hint.files if path.strip()}
                for task in self.state.tasks:
                    task_files = {path.strip() for path in task.files if path.strip()}
                    if hint_files & task_files:
                        target_ids.add(task.id)

        return sorted(target_ids)

    def _prepare_structured_revision_prompt(self) -> str:
        targeted_ids = self._mark_tasks_for_revision()
        return self._build_structured_revision_prompt(targeted_ids)

    def _build_structured_revision_prompt(self, targeted_ids: list[int]) -> str:
        issues_text = "\n".join(f"- {issue}" for issue in self.state.issues)
        target_lines = "\n".join(
            self._revision_target_line(task)
            for task in self.state.tasks
            if task.id in set(targeted_ids)
        )

        return (
            "Previous review found issues that must be fixed.\n"
            f"Target tasks for this revision:\n{target_lines or '- All completed tasks'}\n\n"
            f"Review issues:\n{issues_text}\n\n"
            f"Recent history:\n{self._history_summary(limit=6)}"
        )

    def _revision_target_line(self, task: TaskItem) -> str:
        notes = (
            "; ".join(task.review_notes) if task.review_notes else "No specific note."
        )
        return f"- Task {task.id} ({task.title or task.description}): {notes}"

    def _mark_tasks_for_revision(self) -> list[int]:
        targeted_ids = self._collect_target_task_ids()
        notes_by_task = self._build_revision_notes_by_task(targeted_ids)
        downstream_ids = self._expand_dependent_task_ids(targeted_ids)

        for task in self.state.tasks:
            if task.id in targeted_ids:
                task.status = "needs_revision"
                task.review_notes = notes_by_task.get(task.id, self.state.issues.copy())
            elif task.id in downstream_ids and task.status == "done":
                task.status = "needs_revision"
                task.review_notes = ["Upstream dependency requires revision."]

        tracked_ids = sorted(targeted_ids | downstream_ids)
        self._record_history(
            kind="revision_targeting",
            summary="Prepared targeted revision batch.",
            task_ids=tracked_ids,
            details=[
                self._revision_target_line(task)
                for task in self.state.tasks
                if task.id in tracked_ids
            ],
        )
        return tracked_ids

    def _collect_target_task_ids(self) -> set[int]:
        task_map = {task.id: task for task in self.state.tasks}
        target_ids: set[int] = set()

        for hint in self._normalized_review_task_hints():
            for task_id in hint.task_ids:
                if task_id in task_map:
                    target_ids.add(task_id)
            if hint.files:
                hint_files = {path.strip() for path in hint.files if path.strip()}
                for task in self.state.tasks:
                    task_files = {path.strip() for path in task.files if path.strip()}
                    if hint_files & task_files:
                        target_ids.add(task.id)

        if target_ids:
            return target_ids

        done_or_failed = {
            task.id for task in self.state.tasks if task.status in {"done", "failed"}
        }
        return done_or_failed or {task.id for task in self.state.tasks}

    def _build_revision_notes_by_task(
        self, target_ids: set[int]
    ) -> dict[int, list[str]]:
        notes_by_task: dict[int, list[str]] = {task_id: [] for task_id in target_ids}

        for hint in self._normalized_review_task_hints():
            matched_ids = {
                task_id for task_id in hint.task_ids if task_id in target_ids
            }
            if hint.files:
                hint_files = {path.strip() for path in hint.files if path.strip()}
                for task in self.state.tasks:
                    task_files = {path.strip() for path in task.files if path.strip()}
                    if task.id in target_ids and hint_files & task_files:
                        matched_ids.add(task.id)
            for task_id in matched_ids:
                notes_by_task.setdefault(task_id, []).append(hint.summary)

        return notes_by_task

    def _normalized_review_task_hints(self) -> list[ReviewTaskHint]:
        hints: list[ReviewTaskHint] = []
        for hint in self.state.review_task_hints:
            if isinstance(hint, ReviewTaskHint):
                hints.append(hint)
            elif isinstance(hint, dict):
                hints.append(ReviewTaskHint.model_validate(hint))
        return hints

    def _infer_review_task_hints(
        self, decision: ReviewCrewDecision
    ) -> list[ReviewTaskHint]:
        hints: list[ReviewTaskHint] = []
        combined_text = [decision.summary, *decision.issues]

        for text in combined_text:
            normalized_text = text.lower()
            matched_ids: set[int] = set()
            matched_files: set[str] = set()

            for task in self.state.tasks:
                task_patterns = (
                    rf"\btask\s+#?{task.id}\b",
                    rf"\btask[- ]{task.id}\b",
                    rf"\b#{task.id}\b",
                )
                if any(
                    re.search(pattern, normalized_text) for pattern in task_patterns
                ):
                    matched_ids.add(task.id)

                task_title = (task.title or task.description).strip().lower()
                if task_title and len(task_title) > 8 and task_title in normalized_text:
                    matched_ids.add(task.id)

                for path in task.files:
                    normalized_path = path.strip().lower()
                    if normalized_path and normalized_path in normalized_text:
                        matched_ids.add(task.id)
                        matched_files.add(path)

            if matched_ids or matched_files:
                hints.append(
                    ReviewTaskHint(
                        task_ids=sorted(matched_ids),
                        files=sorted(matched_files),
                        summary=text,
                    )
                )

        deduped: list[ReviewTaskHint] = []
        seen: set[tuple[tuple[int, ...], tuple[str, ...], str]] = set()
        for hint in hints:
            key = (tuple(hint.task_ids), tuple(hint.files), hint.summary)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(hint)

        return deduped

    def _expand_dependent_task_ids(self, seed_ids: set[int]) -> set[int]:
        expanded = set(seed_ids)
        changed = True

        while changed:
            changed = False
            for task in self.state.tasks:
                if task.id in expanded or task.status != "done":
                    continue
                if any(dependency in expanded for dependency in task.dependencies):
                    expanded.add(task.id)
                    changed = True

        return expanded - seed_ids

    # ------------------------------------------------------------------
    # Finalize
    # ------------------------------------------------------------------
    @listen("pass")
    def finalize(self, _decision: str) -> str:
        if self._is_terminal_status():
            logger.info(
                f"[Flow] Skipping finalize because flow is terminal: {self.state.status}"
            )
            return self._terminal_result()
        self._mark_running()
        self._log_event("stage_start", stage="finalize")
        human_gate_message = (
            "About to finalize and write documentation/ADR. This is the last step."
        )
        decision = self._maybe_ask_human(
            "finalize",
            human_gate_message,
        )
        if not decision.proceed:
            if decision.action == "skip-finalize":
                logger.info(
                    "[Flow] Human skipped finalize. Marking flow complete without final docs."
                )
                self.state.final_artifact = (
                    "Finalize skipped by human after review pass."
                )
                self.state.last_stage = "finalize"
                self._maybe_deliver([])
                self.state.status = "completed"
                self._log_event("run_completed", via="skip-finalize")
                self._refresh_debug_report()
                return self.state.final_artifact
            if decision.action == "force-revise":
                logger.info("[Flow] Human reopened work before finalize.")
                reason = (
                    decision.instructions or "Human requested revision before finalize."
                )
                self.state.last_stage = "finalize"
                self.state.review_status = "revise"
                self.state.issues = [reason]
                self.state.review_task_hints = []
                self._refresh_debug_report()
                self._record_history(
                    kind="review_decision",
                    summary="Human reopened work before finalize.",
                    task_ids=sorted(
                        task.id for task in self.state.tasks if task.status == "done"
                    ),
                    details=[reason, "Skipped finalize stage."],
                )
                return "revise"
            if decision.action == "replan":
                logger.info("[Flow] Human requested replanning before finalize.")
                reason = self._set_pending_revision_replan(decision.instructions)
                self.state.last_stage = "finalize"
                self.state.review_status = "revise"
                self.state.issues = [reason]
                self.state.review_task_hints = []
                self._refresh_debug_report()
                self._record_history(
                    kind="human_replanning",
                    summary="Human requested replanning before finalize.",
                    details=[reason, "Skipped finalize stage."],
                )
                return "revise"
            if decision.action == "rerun-review":
                logger.info(
                    "[Flow] Human requested automated review rerun before finalize."
                )
                work_summary = self.state.latest_work_summary
                if not work_summary:
                    reason = (
                        "Cannot rerun review before finalize because no saved "
                        "review input is available."
                    )
                    self.state.last_stage = "finalize"
                    self.state.review_status = "revise"
                    self.state.issues = [reason]
                    self.state.review_task_hints = []
                    self._refresh_debug_report()
                    self._record_history(
                        kind="review_decision",
                        summary="Finalize review rerun unavailable; reopening work.",
                        details=[reason, "Skipped finalize stage."],
                    )
                    return "revise"

                before_review_entry = self._latest_human_feedback_entry(
                    stage="review",
                    gate="before_review",
                    approved=True,
                )
                human_guidance = (
                    before_review_entry.instructions
                    if before_review_entry is not None
                    else None
                )
                review_rerun_guidance = (
                    decision.instructions
                    or "Human requested automated review rerun before finalize."
                )
                self._record_history(
                    kind="review_decision",
                    summary="Human requested automated review rerun before finalize.",
                    details=[review_rerun_guidance],
                )
                review_decision = self._run_verified_review_once(
                    work_summary=work_summary,
                    human_guidance=human_guidance,
                    review_rerun_guidance=review_rerun_guidance,
                )
                logger.info(
                    "[Flow] Review decision: "
                    f"{review_decision.status} | Issues: {len(review_decision.issues)}"
                )
                if review_decision.status == "revise":
                    return "revise"
                direct_flow = cast(Any, self)
                return direct_flow.finalize("pass")
            if decision.action == "target-tasks":
                logger.info(
                    "[Flow] Human selected targeted revision tasks before finalize."
                )
                selected_ids = decision.task_ids or []
                reason = (
                    decision.instructions
                    or "Human selected tasks for targeted revision before finalize."
                )
                selected_files = self._task_files_for_ids(selected_ids)
                self.state.last_stage = "finalize"
                self.state.review_status = "revise"
                self.state.issues = [reason]
                self.state.review_task_hints = [
                    ReviewTaskHint(
                        task_ids=selected_ids,
                        files=selected_files,
                        summary=reason,
                    )
                ]
                self._refresh_debug_report()
                self._record_history(
                    kind="revision_targeting",
                    summary="Human selected targeted revision tasks before finalize.",
                    task_ids=selected_ids,
                    files=selected_files,
                    details=[reason, "Skipped finalize stage."],
                )
                return "revise"
            logger.info("[Flow] Human aborted before finalize.")
            self._mark_human_abort(
                "finalize",
                stage_input=_decision,
                message=human_gate_message,
            )
            return "aborted-by-human"

        worker_tool = self._get_worker("finalize")
        stage_cfg = self.config.get_stage("finalize")

        logger.info(
            f"\n[Flow] finalize using {stage_cfg.worker} (skill: {stage_cfg.skill})"
        )

        human_guidance = (
            f"\nHuman approval instructions:\n- {decision.instructions}\n"
            if decision.instructions
            else ""
        )

        prompt = f"""Create final documentation / ADR for the completed work.

Original request: {self.state.request}
Spec: {self.state.spec[:1500] if self.state.spec else "N/A"}
Revisions used: {self.state.revisions}
Files changed: {self.state.changed_files}
{human_guidance}

Write a concise ADR or completion report suitable for the repository.

Recent flow history:
{self.state.debug_report or self._history_summary(limit=10)}
"""

        # Snapshot around the finalize worker run: adapters under-report
        # changed_files (grok always returns []), and the ADR/report file
        # finalize writes must reach the delivery commit.
        before_finalize = snapshot_workspace(Path(self.state.target_repo))
        result = worker_tool.run(
            task=prompt,
            cwd=self.state.target_repo,
            mode="edit",
            timeout=stage_cfg.timeout,
            model=stage_cfg.model,
        )
        after_finalize = snapshot_workspace(Path(self.state.target_repo))
        finalize_changed = sorted(
            set(diff_workspace_snapshots(before_finalize, after_finalize))
            | set(result.changed_files)
        )

        denied = match_denied(finalize_changed, self._deny_patterns())
        if denied:
            created = sorted(set(after_finalize) - set(before_finalize))
            unrestorable = restore_denied_paths(
                self.state.target_repo, sorted(denied), created=created
            )
            failure = self._denied_failure_result(denied, unrestorable)
            self.state.errors.append(f"Finalize: {failure.error}")
            # Denied paths never reach the delivery commit, restored or not.
            finalize_changed = [path for path in finalize_changed if path not in denied]

        self.state.final_artifact = result.summary or result.raw_output
        self.state.last_stage = "finalize"
        self._maybe_deliver(finalize_changed)
        self.state.status = "completed"
        self._log_event("run_completed")
        self._refresh_debug_report()

        logger.info("[Flow] Flow completed successfully.")
        return self.state.final_artifact or "Flow completed"

    def _delivery_verification_ok(self) -> bool:
        """Whether push/pr may ship: the latest verification round passed.

        Mode-independent on purpose: ``advisory`` only controls whether a
        failure short-circuits the LLM review — shipping unverified work is
        never OK while commands are configured. Empty ``verify.commands``
        means the operator opted out of verification and owns the risk.
        A human force-pass does not override this predicate either.
        """

        if not self.config.verify.get("commands"):
            return True
        runs = self.state.verification_runs
        if not runs:
            return False
        last = runs[-1]
        if isinstance(last, VerificationReport):
            return last.passed
        return bool(last.get("passed"))

    def _delivery_verification_note(self, verification_ok: bool) -> str:
        commands = self.config.verify.get("commands") or []
        if not commands:
            return "Verification: not configured."
        if verification_ok:
            return f"Verification: passed ({len(commands)} command(s))."
        return "Verification: latest round did not pass."

    def _maybe_deliver(self, extra_changed: list[str]) -> None:
        deliver_cfg = self.config.deliver
        if not deliver_cfg.get("enabled", False):
            return
        staged = list(dict.fromkeys([*self.state.changed_files, *extra_changed]))
        # Belt-and-braces: denied paths (e.g. unrestorable ones a failed task
        # still tracked) must never be laundered into the delivery commit.
        denied = match_denied(staged, self._deny_patterns())
        if denied:
            self.state.errors.append(
                "Delivery excluded denied paths: " + ", ".join(sorted(denied))
            )
            staged = [path for path in staged if path not in denied]
        verification_ok = self._delivery_verification_ok()
        report = deliver(
            deliver_cfg,
            target_repo=self.state.target_repo,
            changed_files=staged,
            run_id=self.state.run_id,
            request=self.state.request,
            verification_ok=verification_ok,
            verification_note=self._delivery_verification_note(verification_ok),
        )
        self.state.delivery_report = report
        self._log_event(
            "delivery",
            status=report.status,
            branch=report.branch,
            push=report.push,
            pr=report.pr,
            pr_url=report.pr_url,
        )
        if report.status == "failed":
            # The work exists; delivery is packaging. Record the failure but
            # keep the run completed.
            self.state.errors.append(f"Delivery failed: {report.message}")
        if report.push == "failed":
            self.state.errors.append(f"Delivery push failed: {report.message}")
        if report.pr == "failed":
            self.state.errors.append(f"Delivery PR failed: {report.message}")

    @listen("aborted")
    def aborted(self, _decision: str) -> str:
        logger.info(f"[Flow] Flow stopped at terminal status: {self.state.status}")
        return self._terminal_result()

    @listen("failed")
    def failed(self, _decision: str) -> str:
        logger.info(f"[Flow] Flow stopped at terminal status: {self.state.status}")
        return self._terminal_result()


# Convenience runner for demos / CLI
def run_headless_flow(
    request: str,
    target_repo: str,
    max_revisions: int = 2,
    config: FlowConfig | None = None,
    runs_dir: str | Path | None = None,
) -> FlowState:
    """
    High-level entry point for running the full flow.
    """
    state = FlowState(
        request=request,
        target_repo=target_repo,
        max_revisions=max_revisions,
    )

    run_store: RunStore | None = None
    if runs_dir is not None:
        run_store = RunStore.allocate(Path(runs_dir), request)
        state.run_id = run_store.run_id
        state.run_dir = str(run_store.run_dir)
        state.created_at = datetime.now().isoformat(timespec="seconds")

    flow = CrewAIHeadlessFlow(config=config, run_store=run_store)
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
    flow = CrewAIHeadlessFlow(config=config)
    flow._run_store = run_store  # type: ignore[attr-defined]
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
