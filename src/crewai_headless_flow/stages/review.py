"""Review stage body and helpers (Phase 1 extraction)."""

from __future__ import annotations

import logging
from typing import Any, Literal, cast

from ..review_contract import (
    REVIEW_DECISION_SCHEMA,
    ReviewDecision,
    ReviewTaskHint,
    normalize_review_output,
)
from ..review_crew import ReviewCrewDecision, run_review_crew
from ..verification import VerificationReport, run_verification

logger = logging.getLogger(__name__)


def format_after_review_message(flow, decision: ReviewCrewDecision) -> str:
    issue_lines = (
        "\n".join(f"- {issue}" for issue in decision.issues[:5])
        if decision.issues
        else "- None"
    )
    review_targets = flow._review_target_summary()
    review_target_block = (
        f"Suggested review targets:\n{review_targets}\n\n"
        if review_targets != "- None"
        else ""
    )
    task_catalog = (
        f"\nCurrent task graph:\n{flow._current_task_graph_summary()}\n"
        if flow.state.tasks
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


def build_review_prompt(
    flow,
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
{flow.state.request}

Changed files so far: {flow.state.changed_files}

Planned tasks:
{flow._planned_task_review_context()}
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


def run_verification_round(flow) -> VerificationReport | None:
    """Run the operator-declared verify commands for this review round.

    Returns None when no commands are configured. Runs once per entry
    into the review loop, so every revise cycle (and a human
    rerun-review) re-verifies the tree.
    """

    verify_cfg = flow.config.verify
    if not verify_cfg.get("commands"):
        return None

    flow.state.last_stage = "review"
    # Verify commands are the longest-running Flow-owned subprocesses
    # (test suites, builds) and the likeliest place for an operator
    # Ctrl-C or machine crash; checkpoint first so a resume lands at
    # the review stage instead of replaying completed work.
    flow._refresh_debug_report()
    logger.info(
        f"[Flow] Running {len(verify_cfg['commands'])} verification "
        f"command(s) in {flow.state.target_repo}..."
    )
    report = run_verification(
        verify_cfg,
        cwd=flow.state.target_repo,
        runner=flow._verification_runner,
        config_dir=flow.state.config_dir,
    )
    report.revision = flow.state.revisions
    flow.state.verification_runs.append(report)
    outcome = "passed" if report.passed else "FAILED"
    log = logger.info if report.passed else logger.warning
    log(f"[Flow] Verification {outcome}: {report.message}")
    flow._log_event(
        "verification",
        passed=report.passed,
        mode=report.mode,
        commands=len(report.results),
        message=report.message,
    )
    flow._refresh_debug_report()
    return report


def verification_revise_decision(flow, report: VerificationReport) -> ReviewDecision:
    issues: list[str] = []
    for result in report.results:
        if result.exit_code == 0:
            continue
        suffix = " (timed out)" if result.timed_out else ""
        issue = (
            f"Verification command `{result.command}` exited {result.exit_code}{suffix}"
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


def render_verification_evidence(flow, report: VerificationReport | None) -> str | None:
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


def record_review_decision_state(flow, decision: ReviewDecision) -> None:
    status = decision.status
    issues = decision.issues
    flow.state.review_status = status
    flow.state.issues = issues
    review_task_hints = decision.task_hints
    if not review_task_hints and status == "revise" and flow.state.tasks:
        review_task_hints = flow._infer_review_task_hints(decision)
    flow.state.review_task_hints = review_task_hints
    flow._refresh_debug_report()
    flow._record_history(
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
            {path for hint in review_task_hints for path in getattr(hint, "files", [])}
        ),
        details=[decision.summary, *issues[:3]],
    )


def fail_closed_for_incomplete_structured_tasks(
    flow, decision: ReviewDecision
) -> ReviewDecision:
    if decision.status != "pass" or not flow.state.tasks:
        return decision

    incomplete_task_ids = [
        task.id for task in flow.state.tasks if task.status != "done"
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
                files=flow._task_files_for_ids(incomplete_task_ids),
                summary="Complete the remaining structured tasks before review can pass.",
            ),
        ],
    )


def run_automated_review_once(
    flow,
    *,
    work_summary: str,
    human_guidance: str | None = None,
    review_rerun_guidance: str | None = None,
    verification_evidence: str | None = None,
) -> ReviewDecision:
    worker_tool = flow._get_worker("review")
    stage_cfg = flow.config.get_stage("review")
    prompt = flow._build_review_prompt(
        work_summary=work_summary,
        human_guidance=human_guidance,
        review_rerun_guidance=review_rerun_guidance,
        verification_evidence=verification_evidence,
    )

    flow.state.last_stage = "review"
    crew_cfg = stage_cfg.extra.get("crew", {}) or {}
    if crew_cfg.get("enabled", False):
        try:
            decision = run_review_crew(
                review_context=prompt,
                worker_tool=worker_tool,
                cwd=flow.state.target_repo,
                timeout=stage_cfg.timeout,
                model=stage_cfg.model,
                crew_config=crew_cfg,
                config_dir=flow.state.config_dir,
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
            cwd=flow.state.target_repo,
            mode="inspect",  # Critical: read-only guarantee
            schema=REVIEW_DECISION_SCHEMA,
            timeout=stage_cfg.timeout,
            model=stage_cfg.model,
        )
        decision = normalize_review_output([result.raw_output, result.summary])

    decision = flow._fail_closed_for_incomplete_structured_tasks(decision)
    flow._record_review_decision_state(decision)
    return decision


def run_verified_review_once(
    flow,
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
    verification = flow._run_verification_round()
    if (
        verification is not None
        and not verification.passed
        and verification.mode == "gate"
    ):
        decision = flow._verification_revise_decision(verification)
        flow._record_review_decision_state(decision)
        return decision
    return flow._run_automated_review_once(
        work_summary=work_summary,
        human_guidance=human_guidance,
        review_rerun_guidance=review_rerun_guidance,
        verification_evidence=flow._render_verification_evidence(verification),
    )


def handle_after_review_checkpoint(
    flow,
    *,
    work_summary: str,
    message: str,
    automated_status: Literal["pass", "revise"],
    human_guidance: str | None = None,
) -> tuple[Literal["pass", "revise", "aborted", "rerun-review"], str | None]:
    after_review_decision = flow._maybe_ask_human(
        "review",
        message,
        gate="after_review",
    )
    if not after_review_decision.proceed:
        if after_review_decision.action == "force-revise":
            logger.info("[Flow] Human forced review revise after automated review.")
            flow.state.last_stage = "review"
            flow.state.review_status = "revise"
            if automated_status == "revise":
                if after_review_decision.instructions:
                    flow.state.issues = [
                        *flow.state.issues,
                        f"Human review note: {after_review_decision.instructions}",
                    ]
            else:
                flow.state.issues = [
                    after_review_decision.instructions
                    or "Human requested revision after automated review."
                ]
                flow.state.review_task_hints = []
            flow._refresh_debug_report()
            flow._record_history(
                kind="review_decision",
                summary="Human overrode automated review to revise.",
                details=[
                    f"Automated review was {automated_status}.",
                    *(flow.state.issues[:3]),
                ],
            )
            return "revise", None
        if after_review_decision.action == "replan":
            logger.info("[Flow] Human requested replanning after automated review.")
            reason = flow._set_pending_revision_replan(
                after_review_decision.instructions
            )
            flow.state.last_stage = "review"
            flow.state.review_status = "revise"
            if automated_status == "revise":
                flow.state.issues = [
                    *flow.state.issues,
                    f"Human replan note: {reason}",
                ]
            else:
                flow.state.issues = [reason]
                flow.state.review_task_hints = []
            flow._refresh_debug_report()
            flow._record_history(
                kind="human_replanning",
                summary="Human requested replanning after automated review.",
                details=[f"Automated review was {automated_status}.", reason],
            )
            return "revise", None
        if after_review_decision.action == "force-pass":
            logger.info("[Flow] Human forced review pass after automated review.")
            flow.state.last_stage = "review"
            flow.state.review_status = "pass"
            flow.state.issues = []
            flow.state.review_task_hints = []
            flow._refresh_debug_report()
            flow._record_history(
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
            flow.state.last_stage = "review"
            flow.state.review_status = "revise"
            if automated_status == "revise":
                flow.state.issues = [
                    *flow.state.issues,
                    f"Human target note: {reason}",
                ]
            else:
                flow.state.issues = [reason]
            flow.state.review_task_hints = [
                ReviewTaskHint(
                    task_ids=selected_ids,
                    files=flow._task_files_for_ids(selected_ids),
                    summary=reason,
                )
            ]
            flow._refresh_debug_report()
            flow._record_history(
                kind="revision_targeting",
                summary="Human selected targeted revision tasks after automated review.",
                task_ids=selected_ids,
                files=flow._task_files_for_ids(selected_ids),
                details=[f"Automated review was {automated_status}.", reason],
            )
            return "revise", None
        if after_review_decision.action == "rerun-review":
            logger.info("[Flow] Human requested automated review rerun.")
            review_rerun_guidance = (
                after_review_decision.instructions
                or "Human requested automated review rerun."
            )
            flow._record_history(
                kind="review_decision",
                summary="Human requested automated review rerun.",
                details=[
                    f"Automated review was {automated_status}.",
                    review_rerun_guidance,
                ],
            )
            return "rerun-review", review_rerun_guidance
        logger.info("[Flow] Human aborted after review decision.")
        flow._mark_human_abort(
            "review",
            stage_input=work_summary,
            gate="after_review",
            message=message,
            before_review_instructions=human_guidance,
        )
        return "aborted", None
    if after_review_decision.instructions:
        if automated_status == "revise":
            flow.state.issues = [
                *flow.state.issues,
                f"Human review note: {after_review_decision.instructions}",
            ]
            flow._record_history(
                kind="review_decision",
                summary="Human approved automated review with extra revise guidance.",
                details=[after_review_decision.instructions],
            )
            flow._refresh_debug_report()
        else:
            flow._record_history(
                kind="review_decision",
                summary="Human approved automated review pass with extra guidance.",
                details=[after_review_decision.instructions],
            )
    return automated_status, None


def resume_after_review_checkpoint(
    flow,
    work_summary: str,
    *,
    saved_message: str | None = None,
    saved_before_review_instructions: str | None = None,
) -> Literal["pass", "revise", "aborted"]:
    direct_flow = cast(Any, flow)
    flow.state.latest_work_summary = work_summary
    after_review_message = saved_message or flow.state.aborted_gate_message
    if after_review_message is None:
        aborted_entry = flow._latest_human_feedback_entry(
            stage="review",
            gate="after_review",
            approved=False,
        )
        if aborted_entry is None:
            return direct_flow.review(work_summary)
        after_review_message = aborted_entry.message

    automated_status = flow.state.review_status
    if automated_status not in {"pass", "revise"}:
        return direct_flow.review(work_summary)

    human_guidance = (
        saved_before_review_instructions
        or flow.state.aborted_before_review_instructions
    )
    if human_guidance is None:
        before_review_entry = flow._latest_human_feedback_entry(
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
        result, review_rerun_guidance = flow._handle_after_review_checkpoint(
            work_summary=work_summary,
            message=after_review_message,
            automated_status=cast(Literal["pass", "revise"], automated_status),
            human_guidance=human_guidance,
        )
        if result != "rerun-review":
            return cast(Literal["pass", "revise", "aborted"], result)

        decision = flow._run_verified_review_once(
            work_summary=work_summary,
            human_guidance=human_guidance,
            review_rerun_guidance=review_rerun_guidance,
        )
        automated_status = decision.status
        logger.info(
            f"[Flow] Review decision: {automated_status} | Issues: {len(decision.issues)}"
        )
        after_review_message = flow._after_review_message(decision)


def execute_review(flow, work_summary: str) -> Literal["pass", "revise", "aborted"]:
    if flow._is_terminal_status():
        logger.info(
            f"[Flow] Skipping review because flow is terminal: {flow.state.status}"
        )
        return "aborted"
    flow._mark_running()
    flow._log_event("stage_start", stage="review")
    flow.state.latest_work_summary = work_summary
    stage_cfg = flow.config.get_stage("review")

    logger.info(
        f"\n[Flow] review using {stage_cfg.worker} in INSPECT mode (skill: {stage_cfg.skill})"
    )
    before_review_message = (
        "About to run read-only review stage (review). "
        "This will inspect current changes and may trigger another revision loop."
    )
    human_decision = flow._maybe_ask_human(
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
            flow.state.last_stage = "review"
            flow.state.review_status = "revise"
            flow.state.issues = [reason]
            flow.state.review_task_hints = []
            flow._refresh_debug_report()
            flow._record_history(
                kind="review_decision",
                summary="Review forced to revise by human.",
                details=[reason],
            )
            return "revise"
        if human_decision.action == "replan":
            logger.info(
                "[Flow] Human requested replanning without inspect-mode review."
            )
            reason = flow._set_pending_revision_replan(human_decision.instructions)
            flow.state.last_stage = "review"
            flow.state.review_status = "revise"
            flow.state.issues = [reason]
            flow.state.review_task_hints = []
            flow._refresh_debug_report()
            flow._record_history(
                kind="human_replanning",
                summary="Human requested replanning before automated review.",
                details=[reason, "Skipped inspect-mode review worker."],
            )
            return "revise"
        if human_decision.action == "force-pass":
            logger.info("[Flow] Human forced review pass without inspect-mode worker.")
            flow.state.last_stage = "review"
            flow.state.review_status = "pass"
            flow.state.issues = []
            flow.state.review_task_hints = []
            flow._refresh_debug_report()
            flow._record_history(
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
            flow.state.last_stage = "review"
            flow.state.review_status = "revise"
            flow.state.issues = [reason]
            flow.state.review_task_hints = [
                ReviewTaskHint(
                    task_ids=selected_ids,
                    files=flow._task_files_for_ids(selected_ids),
                    summary=reason,
                )
            ]
            flow._refresh_debug_report()
            flow._record_history(
                kind="revision_targeting",
                summary="Human selected targeted revision tasks before automated review.",
                task_ids=selected_ids,
                files=flow._task_files_for_ids(selected_ids),
                details=["Skipped inspect-mode review worker.", reason],
            )
            return "revise"
        logger.info("[Flow] Human aborted before review.")
        flow._mark_human_abort(
            "review",
            stage_input=work_summary,
            message=before_review_message,
        )
        return "aborted"

    human_guidance = human_decision.instructions
    review_rerun_guidance: str | None = None

    while True:
        decision = flow._run_verified_review_once(
            work_summary=work_summary,
            human_guidance=human_guidance,
            review_rerun_guidance=review_rerun_guidance,
        )
        logger.info(
            f"[Flow] Review decision: {decision.status} | Issues: {len(decision.issues)}"
        )
        result, review_rerun_guidance = flow._handle_after_review_checkpoint(
            work_summary=work_summary,
            message=flow._after_review_message(decision),
            automated_status=decision.status,
            human_guidance=human_guidance,
        )
        if result == "rerun-review":
            continue
        return result


# ------------------------------------------------------------------
# Bounded revise loop
# ------------------------------------------------------------------
