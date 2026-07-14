"""Finalize stage body and delivery glue (Phase 1 extraction)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

from ..delivery import deliver
from ..paths_policy import match_denied, restore_denied_paths
from ..review_contract import ReviewTaskHint
from ..verification import VerificationReport, run_verification
from ..workspace_changes import diff_workspace_snapshots, snapshot_workspace

logger = logging.getLogger(__name__)


def execute_finalize(flow, _decision: str) -> str:
    if flow._is_terminal_status():
        logger.info(
            f"[Flow] Skipping finalize because flow is terminal: {flow.state.status}"
        )
        return flow._terminal_result()
    flow._mark_running()
    flow._log_event("stage_start", stage="finalize")
    human_gate_message = (
        "About to finalize and write documentation/ADR. This is the last step."
    )
    decision = flow._maybe_ask_human(
        "finalize",
        human_gate_message,
    )
    if not decision.proceed:
        if decision.action == "skip-finalize":
            logger.info(
                "[Flow] Human skipped finalize. Marking flow complete without final docs."
            )
            flow.state.final_artifact = "Finalize skipped by human after review pass."
            flow.state.last_stage = "finalize"
            flow._maybe_deliver([])
            flow.state.status = "completed"
            flow._log_event("run_completed", via="skip-finalize")
            flow._refresh_debug_report()
            return flow.state.final_artifact
        if decision.action == "force-revise":
            logger.info("[Flow] Human reopened work before finalize.")
            reason = (
                decision.instructions or "Human requested revision before finalize."
            )
            flow.state.last_stage = "finalize"
            flow.state.review_status = "revise"
            flow.state.issues = [reason]
            flow.state.review_task_hints = []
            flow._refresh_debug_report()
            flow._record_history(
                kind="review_decision",
                summary="Human reopened work before finalize.",
                task_ids=sorted(
                    task.id for task in flow.state.tasks if task.status == "done"
                ),
                details=[reason, "Skipped finalize stage."],
            )
            return "revise"
        if decision.action == "replan":
            logger.info("[Flow] Human requested replanning before finalize.")
            reason = flow._set_pending_revision_replan(decision.instructions)
            flow.state.last_stage = "finalize"
            flow.state.review_status = "revise"
            flow.state.issues = [reason]
            flow.state.review_task_hints = []
            flow._refresh_debug_report()
            flow._record_history(
                kind="human_replanning",
                summary="Human requested replanning before finalize.",
                details=[reason, "Skipped finalize stage."],
            )
            return "revise"
        if decision.action == "rerun-review":
            logger.info(
                "[Flow] Human requested automated review rerun before finalize."
            )
            work_summary = flow.state.latest_work_summary
            if not work_summary:
                reason = (
                    "Cannot rerun review before finalize because no saved "
                    "review input is available."
                )
                flow.state.last_stage = "finalize"
                flow.state.review_status = "revise"
                flow.state.issues = [reason]
                flow.state.review_task_hints = []
                flow._refresh_debug_report()
                flow._record_history(
                    kind="review_decision",
                    summary="Finalize review rerun unavailable; reopening work.",
                    details=[reason, "Skipped finalize stage."],
                )
                return "revise"

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
            review_rerun_guidance = (
                decision.instructions
                or "Human requested automated review rerun before finalize."
            )
            flow._record_history(
                kind="review_decision",
                summary="Human requested automated review rerun before finalize.",
                details=[review_rerun_guidance],
            )
            review_decision = flow._run_verified_review_once(
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
            direct_flow = cast(Any, flow)
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
            selected_files = flow._task_files_for_ids(selected_ids)
            flow.state.last_stage = "finalize"
            flow.state.review_status = "revise"
            flow.state.issues = [reason]
            flow.state.review_task_hints = [
                ReviewTaskHint(
                    task_ids=selected_ids,
                    files=selected_files,
                    summary=reason,
                )
            ]
            flow._refresh_debug_report()
            flow._record_history(
                kind="revision_targeting",
                summary="Human selected targeted revision tasks before finalize.",
                task_ids=selected_ids,
                files=selected_files,
                details=[reason, "Skipped finalize stage."],
            )
            return "revise"
        logger.info("[Flow] Human aborted before finalize.")
        flow._mark_human_abort(
            "finalize",
            stage_input=_decision,
            message=human_gate_message,
        )
        return "aborted-by-human"

    worker_tool = flow._get_worker("finalize")
    stage_cfg = flow.config.get_stage("finalize")

    logger.info(
        f"\n[Flow] finalize using {stage_cfg.worker} (skill: {stage_cfg.skill})"
    )

    human_guidance = (
        f"\nHuman approval instructions:\n- {decision.instructions}\n"
        if decision.instructions
        else ""
    )

    prompt = f"""Create final documentation / ADR for the completed work.

Original request: {flow.state.request}
Spec: {flow.state.spec[:1500] if flow.state.spec else "N/A"}
Revisions used: {flow.state.revisions}
Files changed: {flow.state.changed_files}
{human_guidance}

Write a concise ADR or completion report suitable for the repository.

Recent flow history:
{flow.state.debug_report or flow._history_summary(limit=10)}
"""

    # Snapshot around the finalize worker run: adapters under-report
    # changed_files (grok always returns []), and the ADR/report file
    # finalize writes must reach the delivery commit.
    before_finalize = snapshot_workspace(Path(flow.state.target_repo))
    result = worker_tool.run(
        task=prompt,
        cwd=flow.state.target_repo,
        mode="edit",
        timeout=stage_cfg.timeout,
        model=stage_cfg.model,
    )
    after_finalize = snapshot_workspace(Path(flow.state.target_repo))
    finalize_changed = sorted(
        set(diff_workspace_snapshots(before_finalize, after_finalize))
        | set(result.changed_files)
    )

    denied = match_denied(finalize_changed, flow._deny_patterns())
    if denied:
        created = sorted(set(after_finalize) - set(before_finalize))
        unrestorable = restore_denied_paths(
            flow.state.target_repo, sorted(denied), created=created
        )
        failure = flow._denied_failure_result(denied, unrestorable)
        flow.state.errors.append(f"Finalize: {failure.error}")
        # Denied paths never reach the delivery commit, restored or not.
        finalize_changed = [path for path in finalize_changed if path not in denied]

    flow.state.final_artifact = result.summary or result.raw_output
    flow.state.last_stage = "finalize"
    flow._maybe_deliver(finalize_changed)
    flow.state.status = "completed"
    flow._log_event("run_completed")
    flow._refresh_debug_report()

    logger.info("[Flow] Flow completed successfully.")
    return flow.state.final_artifact or "Flow completed"


def delivery_verification_ok(flow) -> bool:
    """Whether push/pr may ship: the latest verification round passed.

    Mode-independent on purpose: ``advisory`` only controls whether a
    failure short-circuits the LLM review — shipping unverified work is
    never OK while commands are configured. Empty ``verify.commands`` and
    empty ``verify.pre_delivery_commands`` means the operator opted out of
    verification and owns the risk. A human force-pass does not override
    this predicate either.
    """

    verify = flow.config.verify
    if not verify.get("commands") and not verify.get("pre_delivery_commands"):
        return True
    runs = flow.state.verification_runs
    if not runs:
        return False
    last = runs[-1]
    if isinstance(last, VerificationReport):
        return last.passed
    return bool(last.get("passed"))


def run_pre_delivery_verification(flow) -> VerificationReport | None:
    """Run ``verify.pre_delivery_commands`` once before delivery ships.

    Appends to ``verification_runs`` so ``_delivery_verification_ok``
    sees this as the latest round. Returns None when no pre-delivery
    commands are configured.
    """

    verify_cfg = flow.config.verify
    commands = verify_cfg.get("pre_delivery_commands") or []
    if not commands:
        return None

    logger.info(
        f"[Flow] Running {len(commands)} pre-delivery verification "
        f"command(s) in {flow.state.target_repo}..."
    )
    report = run_verification(
        {**verify_cfg, "commands": commands},
        cwd=flow.state.target_repo,
        runner=flow._verification_runner,
        config_dir=flow.state.config_dir,
    )
    report.revision = flow.state.revisions
    flow.state.verification_runs.append(report)
    outcome = "passed" if report.passed else "FAILED"
    log = logger.info if report.passed else logger.warning
    log(f"[Flow] Pre-delivery verification {outcome}: {report.message}")
    flow._log_event(
        "pre_delivery_verification",
        passed=report.passed,
        mode=report.mode,
        commands=len(report.results),
        message=report.message,
    )
    flow._refresh_debug_report()
    return report


def delivery_verification_note(flow, verification_ok: bool) -> str:
    commands = flow.config.verify.get("commands") or []
    pre = flow.config.verify.get("pre_delivery_commands") or []
    if not commands and not pre:
        return "Verification: not configured."
    if verification_ok:
        total = len(commands) + len(pre)
        return f"Verification: passed ({total} command(s) configured)."
    return "Verification: latest round did not pass."


def maybe_deliver(flow, extra_changed: list[str]) -> None:
    deliver_cfg = flow.config.deliver
    if not deliver_cfg.get("enabled", False):
        return
    staged = list(dict.fromkeys([*flow.state.changed_files, *extra_changed]))
    # Belt-and-braces: denied paths (e.g. unrestorable ones a failed task
    # still tracked) must never be laundered into the delivery commit.
    denied = match_denied(staged, flow._deny_patterns())
    if denied:
        flow.state.errors.append(
            "Delivery excluded denied paths: " + ", ".join(sorted(denied))
        )
        staged = [path for path in staged if path not in denied]
    flow._run_pre_delivery_verification()
    verification_ok = flow._delivery_verification_ok()
    report = deliver(
        deliver_cfg,
        target_repo=flow.state.target_repo,
        changed_files=staged,
        run_id=flow.state.run_id,
        request=flow.state.request,
        verification_ok=verification_ok,
        verification_note=flow._delivery_verification_note(verification_ok),
    )
    flow.state.delivery_report = report
    flow._log_event(
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
        flow.state.errors.append(f"Delivery failed: {report.message}")
    if report.push == "failed":
        flow.state.errors.append(f"Delivery push failed: {report.message}")
    if report.pr == "failed":
        flow.state.errors.append(f"Delivery PR failed: {report.message}")
