# Plan: Autonomy Gap Analysis Report for crewai_headless_flow

> Status: approved 2026-07-10. Deliverable: `docs/architecture/autonomy-gap-analysis.md` (analysis only — no source/config/test changes).

## Context

A senior-architect gap analysis of `crewai_headless_flow`: what stands between the current system and a **fully autonomous** end-to-end coding workflow — plan → delegate to headless workers (Codex/Grok/…) → verify → revise → PR-ready change, with HITL only on conditional-policy escalation.

**This is analysis-only.** The single deliverable is a new markdown report at `docs/architecture/autonomy-gap-analysis.md`. No source, config, or test changes.

## Grounding done

Exploration covered (a) Flow core/state/CLI, (b) workers/skills/config, (c) docs/ADRs/tests. Every load-bearing claim was then verified directly against source:

- `state.py:4` docstring claims `@persist` (SQLite) — **the decorator appears nowhere**; state reaches disk only via opt-in `--state-file` after the run (`cli.py:129-130`).
- `flow.py:656` — HITL escalation blocks on `input()`; no TTY → `EOFError` → abort (`flow.py:657-674`), so unattended escalation kills the run. No timeout/notification channel exists.
- `codex.py:57-58` — Codex inspect mode runs against the **real** target repo (trusts `--sandbox read-only`), unlike the other 4 adapters which use disposable copies. Also `tempfile.mktemp` (codex.py:67) and shared `/tmp/codex_last_message.txt` (codex.py:72).
- `codex.py:133` — `tests_passed=False` hardcoded ("Caller decides after running tests") in all 5 adapters; grep confirms **no test/lint/build execution anywhere** in the Flow.
- Grep confirms **zero git write operations** (no add/commit/push/branch/gh pr) — `finalize` (flow.py:3532-3571) only writes an ADR doc via an edit-mode worker; working tree is left dirty, no PR.
- Grep confirms `WorkerTimeout`/`WorkerInvocationError` are **never caught** in `flow.py` or `tools/coder_tool.py` — a real adapter timeout crashes the run instead of routing to the failure/revise path.
- Grep confirms **no `logging` usage** — all diagnostics are `print()`; observability is opt-in `--state-file`/`--debug-report-file` only; no run ID/run history.

Planned-vs-shipped (must be reported as "planned, not shipped", not unknown gaps):
- Conditional HITL Phase 0 **shipped** (hitl_policy seam, 2 triggers, config, overrides, doctor warning — ADR-0003, plan doc all-todos-complete). Later-phase triggers (parallel-conflict, sensitive-paths, review-volume, LLM-judged) **planned** (plan L128-131, ADR-0003 L24).
- HITL↔DMI convergence Phases 2–3 **planned**, gated on ADR-0002's "no Flow-side reading" tension.
- DMI/OpenWiki is deliberately **documentation-only** (ADR-0002; plan L20); gemini native-AGENTS.md is a documented target-repo config gap.
- Notification channels / approval UX / gate timeouts: **not designed anywhere** — genuine gap, not deferred work.
- `invoke-ticket-flow` is an external command driving a *different* repo (`my_crew`); this repo has no ticket ingestion.

## Deliverable structure

Create `docs/architecture/autonomy-gap-analysis.md` containing:

1. **Verdict paragraph** — the platform is a well-tested single-shot orchestrator (~600 offline tests, clean worker abstraction, working HITL seam), but four blockers separate it from the autonomy definition: no objective verification gate, no git/PR delivery, no crash-safe persistence/resume, and HITL escalation that aborts unattended runs. Estimated: the loop's *inner* machinery is done; the *outer* autonomy shell is absent.

2. **Gap table** (gap / dimension / severity / effort / files):

| # | Gap | Dimension | Severity | Effort | Key files |
|---|---|---|---|---|---|
| 1 | No objective verification gate (tests/lint/build never run; acceptance = self-report + LLM review) | Verification | Blocker | M | flow.py:914-926,2604; workers/base.py:24; config/worker.yaml |
| 2 | No git delivery step (no branch/commit/PR; dirty working tree is the output) | Orchestration | Blocker | M | flow.py:3532-3571; workspace_changes.py |
| 3 | No crash-safe persistence/resume; misleading @persist docstring; no run ID/history | Orchestration & state | Blocker | M | state.py:4; flow.py:3606-3710; cli.py:129-130 |
| 4 | Unattended HITL escalation = run abort (blocking input(), no timeout/notification/park-and-resume) | HITL escalation | Blocker | M | flow.py:517-738; hitl_policy.py |
| 5 | WorkerTimeout/WorkerInvocationError uncaught → crash; no retry/backoff/fallback-worker | Error handling | Major | S | flow.py:1548-1554,2952-2958,914-921; tools/coder_tool.py |
| 6 | Serial edits mutate target in place, no rollback on failure | Error handling / safety | Major | S–M | flow.py:2598; workspace_changes.py |
| 7 | Codex inspect not physically isolated; mktemp; shared /tmp file | Safety | Major | S | codex.py:57-72 |
| 8 | Edit-mode blast radius unbounded (all CLIs fully bypassed; no path deny/allow list; apply_changed_files lacks ../ sanitization) | Safety | Major | S–M | codex.py:62 etc.; workspace_changes.py:64-81 |
| 9 | No structured logging/metrics/per-run artifacts by default | Observability | Major | S–M | (new) logging; cli.py:125-130; reporting.py |
| 10 | Adding a 3rd worker touches ≥3 files + 4 diagnostics dicts; binary path not configurable | Worker abstraction | Minor–Major | S | flow.py:95-101,158; diagnostics.py:25-54; workers/__init__.py |
| 11 | Manual-CLI-only trigger; no queue/schedule/webhook; ticket flow lives in another repo | Autonomy surface | Major (Phase 3) | L | cli.py; pyproject.toml:17 |
| 12 | Test gaps: adapter non-zero-exit branch untested; timeout-through-Flow untested; no live codex/grok smokes | Testing | Minor | S | src/tests/test_headless_coders.py |

(Also minor: preflight only warns on dirty target repo, diagnostics may echo CLI stderr into JSON reports without secret redaction, skills/acceptance-criteria are prompt-text conventions.)

3. **Per-blocker/major sections** — current state (file:line), required state, smallest-change sketch. All sketches honor: inspect/edit boundary, Flow ignorance of workers, offline testability (injectable subprocess/git runners, FakeWorker pattern), config-first (new worker.yaml keys over new Python), uv-only. Final sketches:
   - **Gap 1 Verification**: new `verification.py` with injectable `runner`; **top-level** `verify: {commands, mode: gate|advisory, timeout}` block in worker.yaml (sibling of `human_feedback` — runs once per review round, so not per-stage). Called at top of the `review` router (flow.py:2969); gate failure **skips the LLM review** and synthesizes a `revise` decision with command output tails as issues (existing revise loop carries them into the next do_work prompt). Results recorded on new `FlowState.verification_runs` — leave `CoderResult.tests_passed` worker-honest.
   - **Gap 2 Delivery**: new `delivery.py` with injectable git runner; top-level `deliver: {enabled: false, branch_prefix, commit: true, push: false, pr: false, protected_branches}` in worker.yaml; called at end of `finalize` (flow.py:3557-3567). Guardrails: fresh branch only (`{prefix}{run_id}`), never `--force`, refuse protected branches, stage only flow-changed files (`state.changed_files` + snapshot diff) — never `git add -A` (preflight tolerates pre-existing dirt; don't launder it into the commit).
   - **Gap 3 Persistence**: new `run_store.py` — `runs/<run_id>/` with atomic state.json writes at stage transitions (`_checkpoint()` at the same seams that set `last_stage`); add `run_id` to FlowState. **Not** CrewAI `@persist` (would compete with the bespoke resume path at flow.py:3642+); fix the stale state.py:4 docstring. Extend `resume_headless_flow` (flow.py:3613-3624) to accept `status=="running"` (crashed), synthesizing resume stage from `last_stage` and resetting in-progress tasks. Existing checkpoint/resume machinery is reused, not rebuilt.
   - **Gap 4 Escalation**: new `escalation.py` seam parallel to hitl_policy — handler contract is only `ask(request) -> raw answer | None`, replacing exactly the `input()` at flow.py:656; all action parsing stays in flow.py. Channels: `stdin` (default, today's behavior), `file` (write pending_approval.json to run dir, return None → existing EOF path parks the run resumably; no polling), `command` (hook with request JSON on stdin, `timeout_seconds`, `on_timeout: abort|proceed`). Config under `human_feedback.escalation`, validated in `_validate_human_feedback` (config.py:285-306).
   - **Gap 5 Worker exceptions**: single seam — wrap `self.worker.run(...)` in `HeadlessCoderTool.run` (tools/coder_tool.py:84-91), catch `HeadlessCoderError` → failed `CoderResult(exit_code=124, error=...)` so every call site (direct + crews) routes through the existing `result.success` failure path; Flow stays worker-ignorant. Per-stage `retry: {max_attempts, backoff_seconds}` + optional `fallback_worker` in STAGE_EXTRA_SCHEMAS (config.py:192-219), resolved via WORKER_ADAPTERS only. Retry infra errors only, never non-zero exits (those belong to the revise loop). No serial-edit rollback in the minimal change — containment comes from gap 2 (fresh branch) + gap 1 (gate); optional `do_work.isolation: always` knob is a later follow-on gated on gap 9.
   - **Gap 6 Codex isolation**: codex.py only — `mkstemp` instead of `mktemp` (codex.py:67), per-invocation temp file instead of shared `/tmp/codex_last_message.txt` (codex.py:72), and inspect mode runs against a `create_workspace_copy` like the other 4 adapters (keep `--sandbox read-only` inside the copy as defense in depth).
   - **Gap 7 Observability**: piggyback on RunStore — `events.jsonl` per run (serialize existing `FlowHistoryEntry` kinds), always write state.json + debug_report.md into the run dir; keep the CLI flags as extra copies. print→logging conversion deferred as mechanical churn.
   - **Gap 8 Worker registration**: single `WorkerSpec` table in workers/__init__.py `{adapter_cls, binary, help_command, required_flags}`; `WORKER_ADAPTERS` (flow.py:95-101, invariant location preserved) and diagnostics' four dicts (diagnostics.py:25-54) all derived from it — sync by construction. Optional `workers: {name: {binary}}` in worker.yaml; adapters already accept `binary=` kwarg so the zero-arg constructor constraint dissolves.
   - **Gap 9 Mergeback path escape**: ~6 lines in `apply_changed_files` (workspace_changes.py:64-81) — reject absolute/`..` rel_paths + `is_relative_to` resolve check, raise ValueError → task failed.
   - **Gap 10 Blast radius**: constrain at the mergeback/diff boundary the Flow owns (not per-CLI sandbox flags): top-level `paths: {deny: [globs]}` in worker.yaml, enforced in `apply_changed_files` (parallel) and post-`diff_workspace_snapshots` with `git checkout --` restore (serial). Be honest in docs that writes outside target_repo are only contained by the optional isolation knob.

4. **Phased roadmap** (dependency-ordered):
   - **Phase 1 — unattended single-run reliability**: (1) gap 5 exception containment — everything else assumes worker failures don't kill the process; (2) gap 9 path sanitization — prerequisite for every workspace-copy consumer; (3) gap 6 codex temp-file/isolation — removes the concurrency landmine; (4) gap 3 run dir + crash-safe checkpoints + resume-from-crashed; (5) gap 4 escalation channels (depends on run dir); (6) gap 2 in commit-only mode (branch+commit, push/pr off) — ends the dirty-tree problem.
   - **Phase 2 — verification & observability**: (1) gap 1 verify gate (lands in a crash-safe run); (2) enable `deliver.push`/`pr` behind the gate — delivery only ships verified work; (3) gap 7 JSONL event log + always-on artifacts, then mechanical print→logging; (4) gap 10 deny-paths + optional serial isolation; (5) gap 8 WorkerSpec consolidation; (6) gap 12 offline test half (adapter non-zero exit, timeout-through-flow).
   - **Phase 3 — triggering & queueing**: (1) thin `serve`/`enqueue` entrypoint (file-drop queue or webhook wrapper) reusing the `run` path, one run dir per job — enabled by Phase 1 items; (2) concurrency limits + run-history listing over runs/; (3) absorb/replace the external `invoke-ticket-flow` (my_crew) as a first-party trigger; (4) live codex/grok smoke files behind existing markers.

## Verification

- Report exists at `docs/architecture/autonomy-gap-analysis.md`; every file:line citation resolves to the claimed code (all blocker claims verified during planning).
- `git status` shows only the new files.
- The report explicitly labels planned-not-shipped items (HITL later phases, DMI convergence) as such.
