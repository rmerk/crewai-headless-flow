# crewai-headless-flow

A reusable, multi-agent CrewAI Flow that treats agent-skills as operating procedures and delegates code editing/running/testing to pluggable headless coding CLIs.

## Language

**Domain Model Integration**:
A documentation-only reliance on [OpenWiki](https://github.com/langchain-ai/openwiki)'s own `AGENTS.md`/`CLAUDE.md` self-registration in a target repository, which the `cursor`, `claude`, `codex`, and `grok` workers already read natively — no Flow-side code, config, or maintenance. Superseded an earlier `domain-modeling`-skill-based design (Flow-authored `CONTEXT.md`/`docs/adr/`); see `docs/plans/2026-07-06-domain-model-integration.md`.
_Avoid_: Wiki, agent wiki, target-repo domain model (see below, now historical)

**Target-repo domain model** _(historical — see Domain Model Integration)_:
The `CONTEXT.md`/`CONTEXT-MAP.md` glossary and `docs/adr/` decision records that a target repository's own `domain-modeling`-skill-based conventions would hold. No longer written or read by this project's Flow as of the OpenWiki-based revision.
_Avoid_: Wiki, project wiki

**Gate** (human-in-the-loop):
A named checkpoint in the flow where operator input may be requested — one of `before_plan`, `before_do_work`, `before_review`, `after_review`, `before_finalize`. A gate maps to exactly one stage. Whether a gate actually prompts is decided by `hitl_policy.should_prompt()`: in `mode: static` by the gate's boolean, in `mode: conditional` by its [[Trigger]]s. See `src/crewai_headless_flow/hitl_policy.py`.
_Avoid_: hook, checkpoint (reserve "checkpoint" for the abort/resume snapshot)

**Trigger** (conditional human-in-the-loop):
A deterministic, state-derived condition that, under `mode: conditional`, decides whether a [[Gate]] fires. Phase 0 ships two: `repeated_task_failure` (at `before_do_work`) and `approaching_max_revisions` (at `after_review`). Trigger→gate mapping is hardcoded in `hitl_policy._TRIGGERS`; a trigger's config carries only `enabled` plus its own thresholds. When a trigger fires it produces a typed `TriggerReason` persisted on the audit entry.
_Avoid_: rule, condition (unqualified), signal

**Run** (autonomy Phase 1):
One invocation of the flow against a target repository, identified by a `run_id` (`<timestamp>-<request-slug>-<uuid8>`) and homed in `runs/<run_id>/` under `--runs-dir`. A run's identity travels on `FlowState.run_id`/`run_dir`/`created_at` and survives resume.
_Avoid_: job, session, execution (unqualified)

**RunStore** (autonomy Phase 1):
The owner of one run directory (`src/crewai_headless_flow/run_store.py`): atomic `state.json`/`debug_report.md` writes and the `pending_approval.json` location for the file [[Escalation Channel]]. Dependency-light by design (no flow/state imports).
_Avoid_: database, persistence layer

**Checkpoint** (crash-safe persistence):
The `state.json` snapshot the flow writes at every state mutation (piggybacked on `_refresh_debug_report`), plus the synthesized or human-aborted resume point `resume_headless_flow` starts from. A crashed run resumes from its last checkpoint via `synthesize_crash_checkpoint`; a human-aborted run from its `aborted_checkpoint`. Distinct from a [[Gate]].
_Avoid_: save, backup

**Escalation Channel** (human-in-the-loop):
How a fired [[Gate]] reaches a human: `stdin` (blocking terminal prompt), `file` (write `pending_approval.json` to the run dir and park resumably; resume replays the gate and consumes the operator's `answer`), or `command` (configured hook argv; request JSON on stdin, answer on stdout, `on_timeout: abort|proceed`). The seam is `escalation.get_handler(...).ask(request) -> str | None`; `None` parks the run. `hitl_policy` decides *whether* to ask; `escalation` decides *how*.
_Avoid_: notification (unqualified), approval flow

**Delivery** (autonomy Phases 1–2):
The opt-in, Flow-owned git step at the end of `finalize` (`src/crewai_headless_flow/delivery.py`): commit the flow's own changed files onto a fresh `flow/<run_id>` branch, then optionally push it (`deliver.push`) and open a PR via `gh` (`deliver.pr`) — shipping requires the latest [[Verification Gate]] round to have passed whenever verify commands are configured. Never `git add -A`, never force, never a commit on the operator's branch; a push/PR failure never demotes the local commit. Outcome persisted as `FlowState.delivery_report`.
_Avoid_: publish, release

**Verification Gate** (autonomy Phase 2):
The Flow-owned objective check at the top of every review round (`src/crewai_headless_flow/verification.py`): operator-declared `verify.commands` run fail-fast in the target repo, never through a worker adapter. `mode: gate` failures skip the LLM review and become the revise loop's issues; `mode: advisory` becomes review-prompt evidence. Either mode blocks push/PR on failure. Rounds persist on `FlowState.verification_runs`.
_Avoid_: tests (unqualified), CI, validation (unqualified)

**Event Log** (autonomy Phase 2):
The append-only `runs/<run_id>/events.jsonl` stream: one JSON object per line, `{ts, run_id, revision, kind, ...}`, emitted from the history funnel plus lifecycle seams (stage starts, verification, delivery, human feedback/abort, run completion/failure). A resumed run continues the same file. Distinct from the [[Checkpoint]] snapshots, which are whole-file and atomic.
_Avoid_: audit log (that is `human_feedback_log`), trace

**Deny Path** (autonomy Phase 2):
An operator-declared glob (`paths.deny`) no run may change (`src/crewai_headless_flow/paths_policy.py`). Matched with fnmatch semantics where `*` crosses `/` (broad by design); enforced fail-closed at the Flow's merge/diff boundaries and always excluded from [[Delivery]]. File-only config — no CLI override.
_Avoid_: blacklist, ignore pattern (that is snapshot ignore rules)

**Isolation** (do_work knob, autonomy Phase 2):
Where serial (single-task and direct) edits run: `in_place` (default — the target repo, with post-hoc deny restore) or `copy` (a disposable workspace copy with pre-merge deny filtering; a failed or denied edit leaves the target pristine). Parallel batches always use copies.
_Avoid_: sandbox (that is the adapters' CLI flag domain)

**WorkerSpec** (autonomy Phase 2):
The single registration record for one worker (`workers/__init__.py::WORKER_SPECS`): adapter class, default binary, doctor help command, required flags. `WORKER_ADAPTERS` and doctor's worker dicts are derived from it; the top-level `workers:` config block overrides binaries per worker.
_Avoid_: adapter registry (unqualified), plugin table
