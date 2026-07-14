# Autonomy Gap Analysis — crewai_headless_flow

**Date:** 2026-07-10
**Scope:** what stands between the current platform and a fully autonomous end-to-end coding workflow.
**Definition of "fully autonomous":** given a feature request and a target repo (`uv run python -m crewai_headless_flow --request "..." --target-repo ...`), the system can plan, delegate to headless workers (Codex/Grok/…), verify and revise the result, and produce a **PR-ready change without a human in the loop** — escalating to HITL only when the conditional HITL policy triggers.

All file references are to `src/crewai_headless_flow/` unless otherwise noted. Every claim below was verified against source at the cited lines; "planned, not shipped" items are labeled as such and cite the design doc that plans them.

---

## Verdict

The platform's **inner loop is genuinely done** — a clean five-worker adapter abstraction behind a `HeadlessCoder` protocol, structured plan/review contracts that fail closed on malformed output, an isolated-workspace parallel execution path, a working conditional-HITL policy seam, and ~600 offline tests — but the **outer autonomy shell is absent**. Four blockers separate it from the definition above: (1) worker output is accepted on self-report plus an LLM opinion — the Flow never runs tests, lint, or a build against the target repo; (2) there is no git delivery step — no branch, no commit, no PR; the run's output is a dirty working tree plus a report; (3) state is not persisted during a run (the `@persist` claim in `state.py:4` is a stale docstring — the decorator appears nowhere), so a crashed or interrupted run is unrecoverable and there is no run ID or run history; and (4) HITL escalation blocks on stdin `input()` (`flow.py:656`), so in an unattended run every conditional trigger degrades to `EOFError` → full-run abort — there are no notification channels, no approval timeout, no park-and-resume. Additionally, adapter infrastructure exceptions (`WorkerTimeout`, `WorkerInvocationError`) are never caught by the Flow and crash the process instead of routing into the existing failure/revise machinery. The good news: every fix slots into a seam that already exists (the `result.success` failure path, the aborted-checkpoint resume machinery, the `hitl_policy` pattern, the `WORKER_ADAPTERS` registry), so closing the gaps is extension work, not redesign.

---

## What is already solid (context for the gaps)

- **Worker abstraction:** `HeadlessCoder` protocol with a single `run()` contract (`workers/base.py:49-86`), frozen `CoderResult` (`base.py:18-31`), five adapters (codex, grok, claude, gemini, cursor). The Flow's stage logic never branches on worker type; concrete classes appear only in the `WORKER_ADAPTERS` registry (`flow.py:95-101`).
- **Fail-closed contracts:** malformed plan/review output is normalized or forced to `revise` (`flow.py:851-878`, `922`, review/plan contract modules), with strong offline test coverage (`src/tests/test_review_contract.py`, `test_plan_contract.py`).
- **Parallel do_work isolation:** per-task disposable workspace copies with snapshot-diff mergeback and cross-task conflict detection (`workspace_changes.py:21-81`, `flow.py:2657-2742`).
- **Conditional HITL Phase 0 — shipped:** the `should_prompt()` policy seam (`hitl_policy.py:172-206`), two triggers (`repeated_task_failure`, `approaching_max_revisions`), config schema + dotted CLI overrides (`runtime_overrides.py:166-256`), doctor dead-config warnings (`diagnostics.py:583-633`), 40 tests. See ADR-0003 and `docs/plans/2026-07-06-conditional-hitl-phase-0.md` (all todos complete).
- **Offline test discipline:** ~600 tests across 31 files, zero network, zero real CLIs, per the AGENTS.md invariant.

---

## Planned vs. shipped (so these are not misreported as unknown gaps)

| Item | Status | Source |
| --- | --- | --- |
| Conditional HITL Phase 0 (seam, 2 triggers, config, overrides, diagnostics) | **Shipped** | ADR-0003; `docs/plans/2026-07-06-conditional-hitl-phase-0.md` (todos complete) |
| Later-phase HITL triggers: parallel-conflict, sensitive-paths, review-issue-volume, LLM-judged confidence | **Planned, not shipped** | Phase 0 plan L128-131 ("Out of scope"); ADR-0003 L24 |
| Domain Model Integration / OpenWiki pass-through (and HITL↔DMI convergence) | **Rejected** | ADR-0011 (supersedes ADR-0001 / ADR-0002) |
| Escalation channels (stdin/file/command), crash-safe persistence, commit delivery (Phase 1) | **Shipped 2026-07-10** | ADRs 0004–0006 |
| Verification gate, push/PR delivery, JSONL event log, deny paths + serial isolation, WorkerSpec (Phase 2) | **Shipped 2026-07-10** | ADRs 0007–0009 |
| Triggering/queueing (Phase 3) | **Not designed anywhere** — genuine gap (Gap 11) | — |

Note also: the `invoke-ticket-flow` entrypoint is an external command (`~/.claude/commands/invoke-ticket-flow.md`) that drives a **different repository** (`my_crew`); this repo contains no ticket ingestion, and even that flow deliberately halts before commit/PR/Jira writes.

---

## Gap table

| # | Gap | Dimension | Severity | Effort | Key files |
| --- | --- | --- | --- | --- | --- |
| 1 | No objective verification gate — tests/lint/build never run; acceptance = worker self-report + LLM review | Verification gates | **Blocker** | M | `flow.py:914-926`, `flow.py:2604`; `workers/base.py:24`; `config/worker.yaml` |
| 2 | No git delivery — no branch/commit/PR; output is a dirty working tree | Orchestration | **Blocker** | M | `flow.py:3532-3571`; `workspace_changes.py` |
| 3 | No crash-safe persistence or resume; stale `@persist` docstring; no run ID/history | Orchestration & state | **Blocker** | M | `state.py:4`; `flow.py:3606-3710`; `cli.py:129-130` |
| 4 | Unattended HITL escalation aborts the run — blocking `input()`, no timeout/notification/park | HITL escalation | **Blocker** | M | `flow.py:517-738`; `hitl_policy.py` |
| 5 | `WorkerTimeout`/`WorkerInvocationError` uncaught → process crash; no retry/backoff/fallback worker | Error handling | **Major** | S | `flow.py:914-921`, `1548-1554`, `2952-2958`; `tools/coder_tool.py:84-91` |
| 6 | Serial edits mutate the target repo in place with no rollback on failure | Error handling / safety | **Major** | S–M | `flow.py:2598`; `workspace_changes.py` |
| 7 | Codex inspect mode not physically isolated; `tempfile.mktemp`; shared `/tmp/codex_last_message.txt` | Safety | **Major** | S | `workers/codex.py:57-72` |
| 8 | Edit-mode blast radius unbounded — all CLIs run fully bypassed; no path deny-list; mergeback lacks `../` sanitization | Safety | **Major** | S–M | `workers/codex.py:62` (et al.); `workspace_changes.py:64-81` |
| 9 | No structured logging, metrics, or per-run artifacts by default — `print()` only, opt-in state/report files | Observability | **Major** | S–M | `cli.py:125-130`; `reporting.py`; (no `logging` anywhere) |
| 10 | Adding a worker touches ≥3 source files + 4 diagnostics dicts; binary path not configurable | Worker abstraction | Minor–Major | S | `flow.py:95-101`, `158`; `diagnostics.py:25-54`; `workers/__init__.py` |
| 11 | Manual-CLI-only triggering — no queue/scheduler/webhook; no multi-request or cross-repo concurrency story | Autonomy surface | **Major** (Phase 3) | L | `cli.py`; `pyproject.toml:17` |
| 12 | Test gaps: adapter non-zero-exit branch untested; worker-exception-through-Flow untested; no live codex/grok smokes | Testing | Minor | S | `src/tests/test_headless_coders.py` |

Additional minor findings: preflight only **warns** on a dirty target repo (`diagnostics.py:714`) rather than blocking or stashing; doctor/preflight capture raw CLI stderr (bounded to 2000 chars, `diagnostics.py:62`) into JSON reports with no secret redaction; skills and per-task acceptance criteria are prompt-text conventions the LLM may ignore (`skills/loader.py:195-197`, `flow.py:1382-1428`); `worker.yaml`'s `always_approve`/`sandbox` keys are assertions that must match hardcoded adapter behavior, not switches (`config.py:220-225`).

---

## Blocker and major gaps in detail

All "smallest change" sketches below preserve the AGENTS.md invariants: the inspect/edit safety boundary, the Flow's ignorance of concrete workers (only `WORKER_ADAPTERS` at `flow.py:95-101` knows them), 100% offline testability via injectable runners and the existing FakeWorker pattern, and config-first extension through `config/worker.yaml`. uv only.

### Gap 1 — No objective verification gate (Blocker)

**Current state.** Worker output is accepted when `result.success` is true (`flow.py:2604`, `2701`) — a computed property meaning "exit code 0 and no stderr" (`workers/base.py:31`) — plus an LLM review: an inspect-mode worker call against `REVIEW_DECISION_SCHEMA` (`flow.py:914-922`) or an optional Review Crew (`flow.py:899-906`). `CoderResult.tests_passed` is hardcoded `False` by every adapter with the comment "Caller decides after running tests" (`workers/codex.py:133` and equivalents) — but no caller ever runs tests. The do_work prompt asks the model to report "whether tests now pass" (`flow.py:2949`), and nothing verifies the claim. The only objective check is bookkeeping: `_fail_closed_for_incomplete_structured_tasks` forces `revise` if any task isn't marked done (`flow.py:851-878`). There is no test/lint/build subprocess anywhere in the Flow, and no verification-related key in either YAML.

**Required state.** Before the review router can emit `pass`, operator-declared commands (e.g. `uv run pytest -q`, a lint, a build) must run in the target repo and succeed; failures must feed the revise loop as concrete evidence.

**Smallest change.** New `verification.py` module: `run_verification(cfg, cwd, runner=subprocess.run) -> VerificationReport` (per-command `{command, exit_code, output_tail, duration}` — one injectable subprocess boundary, everything else pure). Config: a **top-level** `verify:` block in `worker.yaml` (sibling of `human_feedback`; it runs once per review round, so per-stage keys would be schema surface with no consumer): `verify: {commands: [...], mode: gate|advisory, timeout: 600}`, validated in `config.py` alongside `_validate_human_feedback` (`config.py:285`). Call site: top of the `review` router (`flow.py:2969`) before any worker call. `mode: gate` + failure → **skip the LLM review entirely** and synthesize a `revise` decision with the command output tails as issues — the existing revise loop already carries `state.issues` into the next do_work prompt. `mode: advisory` → append the report to the review prompt as evidence. Record results on a new `FlowState.verification_runs` list; leave `CoderResult.tests_passed` worker-honest (it is a frozen, worker-scoped field). Offline: fake the `runner`; assert gate-failure → revise routing with zero worker calls. Invariant note: verification is a Flow-owned step running operator-declared commands — it is not routed through an adapter and is not part of the read-only inspect stage, so the inspect/edit boundary is untouched.

### Gap 2 — No git delivery step (Blocker)

**Current state.** `finalize` (`flow.py:3532-3571`) runs the finalize worker in edit mode to write an ADR/report document into the repo — that is the entire finalization. Grep confirms zero git write operations anywhere: git usage is read-only preflight probes (`diagnostics.py:665-718`) and `git ls-files` for change snapshotting (`workspace_changes.py:100-125`). A "completed" run leaves the target repo's working tree dirty, on whatever branch it was on. "PR-ready change" does not exist in the platform.

**Required state.** An opt-in delivery step that puts verified changes on a fresh branch with a commit (and, once trusted, a push and PR), with hard guardrails.

**Smallest change.** New `delivery.py`: `deliver(cfg, target_repo, changed_files, run_id, git=_run_git) -> DeliveryReport` with an injectable git runner. Config: top-level `deliver:` in `worker.yaml`: `{enabled: false, branch_prefix: "flow/", commit: true, push: false, pr: false, protected_branches: [main, master]}`. Call site: end of `finalize` (`flow.py:3557-3567`), after the ADR worker run, before `status="completed"`; record the `DeliveryReport` on state. Guardrails: always create a fresh branch (`{prefix}{run_id}`, suffixed on collision); refuse to commit while on a protected branch; **never** pass `--force` to anything; stage only `git add -- <path>` for the flow's own changed files (`state.changed_files` + snapshot-diff results, `flow.py:1555-1557`) — never `git add -A`, because preflight tolerates pre-existing dirt with a warning (`diagnostics.py:714`) and delivery must not launder it into the flow's commit. Offline: real `git` in a `tmp_path` repo is zero-network and fine for branch/commit tests; `push`/`pr` (via `gh`) go through the injected runner and default off. Adapters keep zero git responsibility, so no invariant tension.

### Gap 3 — No crash-safe persistence, no run identity (Blocker)

**Current state.** `state.py:4` says "This state is persisted with @persist (SQLite by default in CrewAI Flows)" — the decorator appears **nowhere** in the codebase. State reaches disk only if the operator passes `--state-file`, and only after the run returns (`cli.py:129-130`). `resume_headless_flow` (`flow.py:3606-3710`) exists and works, but only accepts `status == "aborted_by_human"` with a valid checkpoint (`flow.py:3613-3624`). A process that dies mid-run leaves `status == "running"` in memory and nothing on disk — unrecoverable. There is no run ID, no run directory, no cross-run history.

**Required state.** Every run has an identity and a durable home; state is flushed at each stage transition; a crashed run can be resumed from its last checkpoint.

**Smallest change.** New `run_store.py`: `RunStore(base_dir)` allocating `runs/<run_id>/` (timestamp + request slug + short uuid) with `save_state()` via atomic tmp-write + rename. Add `run_id` to `FlowState`. Wire a `self._checkpoint()` call at the end of each stage method and inside `_mark_human_abort` (`flow.py:715`) and the review-decision recorder — the same seams that already maintain `last_stage`. **Do not adopt CrewAI's `@persist`:** the bespoke resume path replays stage methods directly against a rehydrated state (`flow.py:3642+`), and `@persist` would introduce a second, competing source of truth; instead fix the stale `state.py:4` docstring. Extend `resume_headless_flow` (`flow.py:3613-3624`) to also accept `status == "running"` (a crash), synthesizing the resume stage from `last_stage` and resetting in-progress tasks to pending — everything downstream (checkpoint replay, revise-loop continuation) already exists and is well-tested (`src/tests/test_flow_router_and_loop.py:2408-3035`). CLI: `--runs-dir` (default `./runs`); existing `--state-file`/`--resume-state-file` stay as-is. Offline: `tmp_path` run dirs; simulate a mid-stage kill by resuming from a state saved after stage N.

### Gap 4 — Unattended HITL escalation aborts the run (Blocker)

**Current state.** When a gate fires (static boolean or conditional trigger via `should_prompt`, `flow.py:637-639`), the Flow prints a prompt and blocks on `input("Proceed? [y/N]: ")` (`flow.py:656`). With no TTY, `input()` raises `EOFError`, caught at `flow.py:657-674` → an abort feedback entry → `_mark_human_abort` (`flow.py:715-738`) → `status="aborted_by_human"`. **Net effect: every conditional escalation in an unattended run terminates the entire flow.** There is no timeout, no notification channel (no email/Slack/webhook code exists), and no approval UX beyond stdin. This is a genuine gap: neither Phase 0 nor any later-phase plan designs notification/approval/timeout — the Phase 0 plan's out-of-scope list (L128-131) covers only additional *triggers*.

**Required state.** A fired trigger in an unattended run must reach a human out-of-band and park the run resumably — or time out to a configured default — instead of killing it.

**Smallest change.** New `escalation.py` seam, deliberately parallel to `hitl_policy`: `get_handler(hf_cfg) -> EscalationHandler` whose only contract is `ask(request: EscalationRequest) -> str | None` (the raw answer string). It replaces exactly the `input()` at `flow.py:656`; all action parsing (`_parse_human_feedback_action`, advanced actions, instruction capture) stays in `flow.py` unchanged. Channels: `stdin` (default — today's behavior); `file` — write `pending_approval.json` into the run dir (Gap 3) and return `None`, so the flow takes the existing EOF path and **parks resumably via the already-working `aborted_checkpoint` + resume machinery** (no polling, no long-lived process; the operator answers by editing the file and re-running with `--resume-state-file`); `command` — run a configured hook with the request JSON on stdin and parse the answer from stdout, with `timeout_seconds` and `on_timeout: abort|proceed`. The command channel is where Slack/email/webhook notification plugs in without the platform growing network code — preserving offline testability. Config under `human_feedback.escalation: {channel, command, timeout_seconds, on_timeout}`, validated in `_validate_human_feedback` (`config.py:285-306`). Offline: stdin tests already monkeypatch `input`; the file channel is pure file IO; the command channel injects a fake runner.

### Gap 5 — Worker infrastructure exceptions crash the Flow (Major)

**Current state.** Adapters raise `WorkerTimeout` / `WorkerInvocationError` on hang or launch failure (`workers/base.py:89-98`, `workers/codex.py:109-112`), but `flow.py` imports only `CoderResult`/`HeadlessCoder` (`flow.py:85`) and neither it nor `tools/coder_tool.py` contains a single `except` for them (grep-verified). The direct worker calls at `flow.py:914-921` (review), `1548-1554` (serial do_work), and `2952-2958` (unstructured do_work) are unguarded — a real timeout propagates to the CLI top-level and kills the run instead of becoming a task failure. There is no retry, no backoff, and no fallback-to-another-worker anywhere; the Flow's failure logic keys entirely on `result.success`, i.e. it assumes adapters *return* failure, but they *raise* on infrastructure errors.

**Required state.** Infrastructure failures become ordinary task failures that the existing revise/replan/HITL machinery handles; transient failures get bounded retries; optionally a stage can fail over to a second worker.

**Smallest change.** One seam: wrap `self.worker.run(...)` inside `HeadlessCoderTool.run` (`tools/coder_tool.py:84-91`) with `except HeadlessCoderError as exc: return CoderResult(summary="", exit_code=124, error=f"{type(exc).__name__}: {exc}")`. Every call site — direct calls and all crews, since they receive the same tool — is covered at once, and the Flow stays worker-ignorant: it only ever sees `result.success == False`, which the existing failure paths already handle. Then add per-stage `retry: {max_attempts: 1, backoff_seconds: 0}` and optional `fallback_worker` to `STAGE_EXTRA_SCHEMAS` (`config.py:192-219`); `_setup_workers` (`flow.py:145-164`) resolves the fallback through `WORKER_ADAPTERS` only. Retry fires **only** on infrastructure exceptions, never on non-zero exits — semantic failure belongs to the revise loop. `sleep_fn` injectable for offline tests. This change also closes half of Gap 12 (it defines the behavior the missing tests should assert).

### Gap 6 — Serial edits mutate the target in place, no rollback (Major)

**Current state.** Single-task (serial) do_work runs the edit-mode worker directly in the target repo (`cwd=self.state.target_repo`, `flow.py:2598`). On failure there is no rollback — the repo is left dirty with partial edits; changes are only *detected* via SHA-256 snapshot diff (`flow.py:1499-1557`, `workspace_changes.py:39-61`). Only the parallel path enjoys isolation (disposable copies, mergeback on success, `flow.py:2738-2742`).

**Required state.** A failed unattended run must not leave the target repo in an ambiguous half-edited state — or the state must be precisely bounded and reported.

**Smallest change.** Deliberately *not* a rollback mechanism in the first pass: partial edits being visible to review/revise is the current design, and containment comes from Gap 2 (all flow changes land on a fresh branch, so `git diff` against the base is always clean evidence) plus Gap 1 (the gate refuses to pass unverified trees). The optional deeper fix is a `do_work.isolation: always` knob reusing the existing `create_workspace_copy` + `apply_changed_files` machinery for serial tasks too — gated on Gap 9's path sanitization landing first.

### Gap 7 — Codex inspect mode is not physically isolated (Major)

**Current state.** Codex is the only adapter whose inspect mode runs against the **real** target directory, trusting the CLI's `--sandbox read-only` flag (`workers/codex.py:57-58`); grok/claude/cursor/gemini all copy the tree to a disposable directory first (e.g. `workers/grok.py:56-62`). If that flag's semantics regress in any codex-cli release, a review pass can mutate the real repo. Two adjacent defects: the schema temp file uses deprecated, race-prone `tempfile.mktemp` (`codex.py:67`), and the fallback output file is a **hardcoded shared path** `/tmp/codex_last_message.txt` (`codex.py:72`) that collides across concurrent runs — a landmine for any Phase 3 concurrency.

**Required state.** Inspect mode is physically incapable of mutating the target repo for every adapter; no shared temp paths.

**Smallest change.** All in `workers/codex.py`: (a) `tempfile.mkstemp` instead of `mktemp`; (b) a per-invocation `mkstemp` path for `--output-last-message`, read back and unlinked; (c) for `mode == "inspect"`, run against a `create_workspace_copy` of the target like the other four adapters, keeping `--sandbox read-only` inside the copy as defense in depth. Copy cost on large repos is acceptable and the existing ignore rules trim it. Offline: `test_headless_coders.py` already fakes `subprocess.run`; assert `--cd` points into a temp copy and no shared `/tmp` path appears in argv.

### Gap 8 — Edit-mode blast radius is unbounded (Major)

**Current state.** Every adapter runs its CLI in fully-bypassed mode for edits: Codex `--dangerously-bypass-approvals-and-sandbox` (`codex.py:62`), Grok `--always-approve`, Claude `bypassPermissions`, Cursor `--force --trust`, Gemini `--approval-mode yolo`. Nothing constrains writes to inside `target_repo` — a worker writing an absolute path or `../` escapes it, and no path allowlist/denylist exists anywhere (grep-verified). Separately, `apply_changed_files` (`workspace_changes.py:64-81`) joins `dest_root / rel_path` with no sanitization against absolute or `..` components — a latent path escape in the mergeback itself.

**Required state.** The Flow-owned boundary (mergeback/diff) enforces path constraints; the honest limits of post-hoc detection are documented.

**Smallest change.** Two parts. **(Gap 9, ~6 lines, do first):** in `apply_changed_files`, reject any `rel_path` that `is_absolute()` or contains `..` in its parts, plus a belt-and-braces `dest.resolve().is_relative_to(dest_root.resolve())` check; raise `ValueError` so the caller marks the task failed rather than silently skipping. **(Deny-list):** accept that the CLIs run bypassed — that is the product — and constrain at the boundary the Flow owns: a top-level `paths: {deny: [globs]}` in `worker.yaml` (deny-only to start; allowlists cause false failures), enforced in `apply_changed_files` for the parallel path and after `diff_workspace_snapshots` (`flow.py:1555-1557`) for the serial path, restoring denied files via `git checkout -- <path>` (or delete-if-untracked). Writes entirely outside `target_repo` are not detectable post-hoc; the honest containment for that is the optional `do_work.isolation` knob (Gap 6) — the docs should say so rather than implying a denylist covers it.

### Gap 9 — Observability: `print()` only, artifacts opt-in (Major)

*(Numbered 9 in the table; the mergeback sanitization above is tracked inside Gap 8's fix ordering.)*

**Current state.** No `logging` usage anywhere in the core package (grep-verified) — all diagnostics are `print()` with `[Flow]`/`[Human Feedback]` prefixes. A rich markdown execution report exists (`reporting.py:18-246`) and is refreshed on nearly every state mutation, but it and the state snapshot reach disk **only** if the operator passes `--debug-report-file`/`--state-file` (`cli.py:125-130`). No run ID, no metrics, no structured event stream. "Why did run X fail?" is answerable a day later only if the operator remembered the flags — and even then, nothing correlates multiple runs.

**Required state.** Every run always leaves a durable, correlated trail: state, report, and a structured event log.

**Smallest change.** Piggyback on Gap 3's `RunStore`: a `log_event(kind, **fields)` appending JSONL to `runs/<run_id>/events.jsonl`, called from exactly the seams `_checkpoint()` touches (stage transitions, task complete/fail, review decisions, escalations, verification, delivery). `FlowHistoryEntry` (`state.py:116`) already defines most event kinds — serialize those rather than inventing a new taxonomy. Always write `state.json` and `debug_report.md` into the run dir; keep the CLI flags as optional extra copies. A wholesale `print()`→`logging` conversion is churn, not a gap-closer — defer it as a mechanical follow-up.

### Gap 10 — Worker registration is scattered (Minor–Major)

**Current state.** Adding a worker requires: the adapter file, an export in `workers/__init__.py`, an entry in `WORKER_ADAPTERS` (`flow.py:95-101`), and entries in **four** parallel dicts in `diagnostics.py:25-54` (`SUPPORTED_WORKERS`, `WORKER_BINARIES`, `WORKER_HELP_COMMANDS`, `WORKER_REQUIRED_FLAGS`) or `doctor` misreports it. The Flow instantiates adapters with zero args (`flow.py:158`), so binary paths are not configurable despite every adapter accepting a `binary=` kwarg (`codex.py:36`).

**Required state.** One place to register a worker; binaries configurable from YAML.

**Smallest change.** A single `WorkerSpec` table in `workers/__init__.py` — `{adapter_cls, binary, help_command, required_flags}` per worker. `WORKER_ADAPTERS` keeps its name and location (`flow.py:95-101` — the invariant's stated home) but is *derived* from the table, as are all four diagnostics dicts — in sync by construction. Optional top-level `workers: {codex: {binary: "/opt/bin/codex"}}` in `worker.yaml`; `_setup_workers` passes `adapter_cls(binary=...)`. Adding a worker becomes: adapter file + one spec entry.

### Gap 11 — Autonomy surface: manual CLI only (Major, Phase 3)

**Current state.** The only trigger is `crewai-headless-flow run ...` (`pyproject.toml:17`, `cli.py:42-62`; subcommands `run`/`doctor`/`preflight`). No server, daemon, queue consumer, scheduler, or webhook exists (grep-verified). No multi-request queueing; concurrent runs against different repos would today collide on Codex's shared `/tmp` file (Gap 7) and have no run identity to keep artifacts apart (Gap 3). Ticket ingestion lives in a different repository entirely.

**Required state.** Requests can arrive from a queue/schedule/webhook; N runs proceed concurrently with isolated identities and artifacts.

**Smallest change (after Phases 1–2).** A thin `serve`/`enqueue` entrypoint — a file-drop queue directory or minimal webhook wrapper — that shells into the existing `run` path with one run dir per job. This is deliberately last: it is *enabled by* run identity (Gap 3), TTY-free escalation (Gap 4), no shared temp files (Gap 7), and per-run branches (Gap 2). Add a concurrency limit and a run-history listing over `runs/`. Then absorb or replace the external `invoke-ticket-flow` as a first-party trigger.

### Gap 12 — Testing: failure paths under-covered where it matters most (Minor)

**Current state.** The offline suite is strong on contract failures (malformed output → fail-closed), replan recovery, HITL gates, and resume — but: every adapter test in `test_headless_coders.py` uses `returncode=0`, so the non-zero-exit branch (`codex.py:136`) is untested at the adapter level; no test drives a raised `WorkerTimeout` through a Flow run (consistent with the Flow not handling it — Gap 5); and no `test_live_codex_smoke.py`/`test_live_grok_smoke.py` exist despite the markers being registered (`pyproject.toml:36-46`), while claude/gemini/cursor each have one.

**Smallest change.** Adapter tests with `returncode!=0` asserting the `CoderResult(error=stderr)` mapping; once Gap 5 lands, FakeWorker-raises-`WorkerTimeout` tests asserting conversion/retry/fallback; opt-in live smoke files for codex and grok behind the existing markers.

---

## Phased roadmap (dependency-ordered)

### Phase 1 — Unattended single-run reliability — SHIPPED (2026-07-10, ADRs 0004–0006)

*A run must survive its own infrastructure before verification or triggering matter.*

1. **Gap 5 — exception containment at the tool seam** (`tools/coder_tool.py`). Everything downstream assumes worker failures don't kill the process.
2. **Gap 8(a) — mergeback path sanitization** (`workspace_changes.py:64-81`). Tiny, and a prerequisite for every subsequent workspace-copy consumer.
3. **Gap 7 — Codex temp files + inspect isolation** (`workers/codex.py`). Independent, small, removes the concurrency landmine early.
4. **Gap 3 — run dir, crash-safe checkpoints, resume-from-crashed** (`run_store.py`, `state.py`, `flow.py:3613-3624`). Provides the `run_id`/run dir that items 5, Phase 2's observability, and delivery all consume.
5. **Gap 4 — escalation seam** (`escalation.py`; file channel parks via the existing checkpoint machinery). Depends on the run dir.
6. **Gap 2 in commit-only mode** (`delivery.py`; branch + commit, `push`/`pr` off). Ends the dirty-tree problem so every unattended run is cleanly inspectable; full delivery trust waits for the verify gate.

**Exit criterion:** a no-TTY run either completes on its own branch, parks resumably awaiting approval, or fails with a resumable checkpoint — it never crashes unrecoverably and never leaves ambiguous state.

### Phase 2 — Verification & observability — SHIPPED (2026-07-10, ADRs 0007–0009)

*Make "completed" mean something, and make every run diagnosable after the fact.*

1. **Gap 1 — verify gate** (`verification.py`, `verify:` config, review-router call site). After Phase 1 so verification failures land in a crash-safe, resumable run.
2. **Enable `deliver.push`/`pr` behind the gate** — delivery now only ships verified work.
3. **Gap 9 — JSONL event log + always-on run artifacts**; then the mechanical `print()`→`logging` conversion.
4. **Gap 8(b) — deny-path enforcement**; optional serial `do_work.isolation` knob (closes Gap 6's deeper fix).
5. **Gap 10 — WorkerSpec consolidation + configurable binaries.**
6. **Gap 12 (offline half)** — adapter non-zero-exit tests, worker-exception-through-Flow tests, verification/delivery/escalation fakes.

**Exit criterion:** `status == "completed"` implies the operator's verify commands passed on the delivered branch, and `runs/<run_id>/` answers "why did run X fail?" without any opt-in flags.

### Phase 3 — Triggering & queueing — CORE SHIPPED (2026-07-11, ADR 0010)

*Only now is a standing autonomous service safe to build.*

1. **Gap 11 — thin `serve`/`enqueue` entrypoint** (file-drop queue or webhook wrapper) reusing the existing `run` path, one run dir per job. — SHIPPED (`job_queue.py`; file-drop queue, atomic rename claims, run subprocesses, ADR 0010)
2. **Concurrency limits + run-history listing** over `runs/`. — SHIPPED (`serve --max-concurrent`; `runs` via `run_store.summarize_runs`)
3. **First-party ticket trigger** — absorb or replace the external `invoke-ticket-flow` (`my_crew`) integration. — DEFERRED: its interface is exactly an `enqueue` call; which tracker/credentials/mapping to use is an operator decision outside this repo's core (see ADR 0010).
4. **Gap 12 (live half)** — opt-in codex/grok smoke files behind the existing markers. — SHIPPED (`test_live_codex_smoke.py`, `test_live_grok_smoke.py`)

**Exit criterion:** a feature request dropped in the queue produces, without human touch, a pushed branch (or PR) that passed the verify gate — or a parked, notified escalation awaiting one human decision.

---

## Constraints adherence

Every recommendation above: keeps inspect stages read-only and edit stages non-interactive (verification and delivery are Flow-owned, never routed through adapters; Codex inspect gains the same physical isolation the other adapters already have); keeps the Flow ignorant of concrete workers (the only concrete-worker knowledge stays in `WORKER_ADAPTERS`/`WorkerSpec`; exception handling and retries live at the tool seam); stays 100% offline-testable (injectable `runner`/`git`/`sleep_fn` boundaries, `tmp_path` repos, the existing FakeWorker pattern — the `command` escalation channel keeps network code out of the platform entirely); extends `config/worker.yaml` (`verify:`, `deliver:`, `retry:`, `fallback_worker`, `human_feedback.escalation`, `paths.deny`, `workers.<name>.binary`) rather than adding Python where config suffices; and introduces no package manager beyond uv.
