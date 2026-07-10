---
name: Autonomy Phase 1 — Unattended Single-Run Reliability
overview: Implement Phase 1 of docs/architecture/autonomy-gap-analysis.md — six dependency-ordered items (Gaps 5, 8a, 7, 3, 4, 2) so a no-TTY run either completes on its own branch, parks resumably awaiting approval, or fails with a resumable checkpoint; it never crashes unrecoverably and never leaves ambiguous state.
todos:
  - id: gap5-containment
    content: Contain WorkerTimeout/WorkerInvocationError at the HeadlessCoderTool seam (exit_code 124 CoderResult); per-stage retry {max_attempts, backoff_seconds} + fallback_worker in STAGE_EXTRA_SCHEMAS; _setup_workers resolves fallback via WORKER_ADAPTERS
    status: completed
  - id: gap8a-sanitize
    content: apply_changed_files rejects absolute/../resolve-escaping rel_paths (validate whole list before applying); parallel mergeback call site routes ValueError into failed_outcomes
    status: completed
  - id: gap7-codex
    content: codex.py mkstemp for schema file; per-invocation --output-last-message temp path with read-back fallback; inspect mode runs against a codex-inspect- disposable copy (grok pattern), keeping --sandbox read-only as defense in depth
    status: completed
  - id: gap3-runstore
    content: run_store.py (allocate/attach, atomic save_state/save_debug_report); FlowState run_id/run_dir/created_at; checkpoint piggybacked in _refresh_debug_report; run/resume_headless_flow runs_dir param; resume accepts status=="running" via synthesize_crash_checkpoint; CLI --runs-dir (default ./runs); fix stale @persist docstring in state.py
    status: completed
  - id: gap4-escalation
    content: escalation.py seam (EscalationRequest, EscalationHandler protocol, stdin/file/command channels, get_handler); _maybe_ask_human swaps input() for handler.ask(); human_feedback.escalation config schema + validation + dotted override root
    status: completed
  - id: gap2-delivery
    content: delivery.py commit-only git delivery (DeliveryReport pydantic model, fresh {branch_prefix}{run_id} branch, per-path staging, never --force/add -A, push/pr validated-but-ignored); top-level deliver config block + --override-deliver family; finalize wiring with snapshot diff at both completion tails
    status: completed
  - id: docs
    content: ADRs 0004 (RunStore checkpoints, @persist rejection), 0005 (escalation channel seam), 0006 (Flow-owned git delivery); README/DESIGN/AGENTS/CONTEXT/operator-playbook sweep
    status: completed
  - id: tests
    content: Offline tests per item — test_coder_tool_containment.py, test_workspace_changes.py, codex updates in test_headless_coders.py, test_run_store.py, test_crash_resume.py, test_escalation.py, test_delivery.py (real git in tmp_path) — plus flow/config/CLI integration additions; full suite green after every commit
    status: completed
isProject: false
---

# Autonomy Phase 1 — Unattended Single-Run Reliability

## Background

`docs/architecture/autonomy-gap-analysis.md` (2026-07-10) found that the platform's inner loop is solid but the outer autonomy shell is absent: a worker timeout crashes the process (Gap 5), mergeback has a latent path escape (Gap 8a), Codex inspect mode is not physically isolated and uses shared/racy temp files (Gap 7), a crashed run is unrecoverable with no run identity (Gap 3), an unattended HITL trigger degrades to `EOFError` → full-run abort (Gap 4), and a "completed" run leaves the target repo's working tree dirty (Gap 2).

This plan executes the analysis's **Phase 1** roadmap exactly, in its dependency order. Phases 2 (verification gate, observability, deny-paths, WorkerSpec) and 3 (triggering/queueing) are out of scope.

**Exit criterion (from the analysis):** a no-TTY run either completes on its own branch, parks resumably awaiting approval, or fails with a resumable checkpoint — it never crashes unrecoverably and never leaves ambiguous state.

## Design decisions

- **Gap 5 containment lives at `HeadlessCoderTool.run`**, not per call site: every direct call and every crew receives the same tool, so one `except HeadlessCoderError` covers all of them and the Flow stays worker-ignorant — it only ever sees `result.success == False`, which existing failure paths handle. Retry fires **only** on infrastructure exceptions (never on non-zero exits — semantic failure belongs to the revise loop); `sleep_fn` is injectable for offline tests. The synthetic failure uses `exit_code=124` (timeout convention) and `error="<ExceptionType>: <message>"`.
- **Retry/fallback are per-stage extras** (`retry: {max_attempts, backoff_seconds}`, `fallback_worker`) in `STAGE_EXTRA_SCHEMAS`, which makes them `--override-stage-extra`-addressable with zero override-layer changes. `fallback_worker` resolution happens in `_setup_workers` via `WORKER_ADAPTERS` (config.py cannot import it — circular).
- **Gap 8a validates the whole `changed_files` list before applying anything** — no partial application from a poisoned list. The parallel mergeback call site converts the `ValueError` into an ordinary failed outcome so the existing replan/mark-failed machinery takes over.
- **Gap 7 mirrors the grok disposable-copy pattern privately in codex.py** (prefix `codex-inspect-`) rather than unifying all five adapters' copy helpers — consolidation is a separate follow-up. `--sandbox read-only` is kept inside the copy as defense in depth.
- **Gap 3 rejects CrewAI's `@persist`** (the stale `state.py` docstring is corrected): the bespoke resume path replays stage methods against a rehydrated state, and `@persist` would be a second, competing source of truth. Instead, a `RunStore` writes `state.json` + `debug_report.md` via atomic tmp-write + `os.replace`, **piggybacked inside `_refresh_debug_report`** — already called at every stage tail, review decision, task completion/failure, human-feedback record, and `_mark_human_abort`, so checkpoint coverage stays in sync with report coverage by construction. Checkpoint write failures warn and never kill the run.
- **`last_stage` is not "last completed stage"** — structured do_work and review both set it *before* their worker calls. `synthesize_crash_checkpoint` therefore re-runs the named stage (resetting `in_progress` tasks to `pending`) rather than naively advancing to the next one; `review` resumes from `latest_work_summary`; `finalize` + `review_status=="revise"` resumes the revise loop.
- **Gap 4's escalation contract is `ask(request) -> str | None`**: the raw answer string, or `None` meaning "no answer available", which routes into the exact code path `EOFError` takes today — `_record_human_feedback(response="no-input")` → `_mark_human_abort` → parked resumably via the existing aborted-checkpoint machinery. All action parsing stays in `flow.py` untouched. The **file channel** writes `pending_approval.json` and parks; on resume the gate replays and the handler **consumes** an operator-written `"answer"` field (renaming to `answered_approval.json`), closing the approval loop without any long-lived process. The **command channel** (configured argv, request JSON on stdin, answer on stdout, `timeout_seconds`, `on_timeout: abort|proceed`) is where Slack/email/webhook notification plugs in — network code never enters the platform.
- **Gap 2 delivery is Flow-owned and commit-only**: fresh `{branch_prefix}{run_id}` branch, per-path `git add --` of only the flow's own changed files (never `add -A` — preflight tolerates pre-existing dirt and delivery must not launder it), no `--force` anywhere, `protected_branches` refusal, collision suffixing, unborn/detached HEAD supported. `push`/`pr` keys are validated but **ignored with a log line** until Phase 2's verify gate. Finalize gains a workspace snapshot diff around its worker call (adapters under-report `changed_files`; without the diff the delivered commit would miss the ADR finalize just wrote). Delivery failure records an error + `failed` report but keeps the run `completed` — the work exists; delivery is packaging. A delivered run ends checked out on the `flow/<run_id>` branch.

## Out of scope

- Verification gate (`verify:`), push/PR delivery, JSONL event log, deny-path enforcement, serial `do_work.isolation`, WorkerSpec consolidation — Phase 2.
- serve/enqueue entrypoint, concurrency limits, ticket trigger, live smoke tests — Phase 3.
- Unifying the five adapters' private disposable-copy helpers — mechanical follow-up.
