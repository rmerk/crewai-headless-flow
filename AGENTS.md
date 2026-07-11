# AGENTS.md — crewai-headless-flow

Guidelines for AI agents (and humans using agents) working in this repository.

## Project Overview

**crewai-headless-flow** is a reusable, multi-agent CrewAI Flow that treats **Addy Osmani's agent-skills** as operating procedures ("the how") and delegates actual code editing, running, and testing to **pluggable headless coding CLIs** ("the hands").

- **Core idea**: The Flow provides the orchestration and state machine. Skills provide consistent methodology. Workers (CodexAdapter, GrokAdapter, ClaudeAdapter, GeminiAdapter, CursorAdapter) provide the execution capability.
- **Primary reusability levers**: Two small YAML files (`config/skills.yaml` and `config/worker.yaml`). Changing procedures or which brain actually edits the code requires **zero Python changes**.
- **Safety model**: Inspect/review stages are always read-only. Edit stages are non-interactive. Grok inspect mode uses disposable copies; Claude inspect mode uses a disposable copy plus `dontAsk`.
- **Testing guarantee**: 100% of core behavior is offline-testable (`pytest -m offline`). Live CLI smoke tests are opt-in and gated.

**GitHub**: https://github.com/rmerk/crewai-headless-flow  
**Current version**: 0.1.0 (as of 2026-06-01 publish)

## Architecture — The Two Pillars

### 1. Skills as Operating Procedures
- Vendored copy of agent-skills lives in `vendor/agent-skills/skills/`.
- `src/crewai_headless_flow/skills/loader.py` parses `SKILL.md` files and extracts the core procedural guidance.
- The selected skill for each stage is injected into prompts via `HeadlessCoderTool`.
- Current mapping (see `config/skills.yaml`):
  - `plan` → `planning-and-task-breakdown`
  - `do_work` → `incremental-implementation`
  - `review` → `code-review-and-quality`
  - `finalize` → `documentation-and-adrs`

### 2. Pluggable Headless Coders
- Protocol defined in `workers/base.py` (`HeadlessCoder`).
- Four production adapters:
  - `CodexAdapter`: Uses native `--sandbox` + `--output-schema`.
  - `GrokAdapter`: Uses disposable copies for safe inspect mode + prompt-based structured output + one repair retry.
  - `ClaudeAdapter`: Uses disposable copies plus `--permission-mode dontAsk` for inspect mode, real target repositories plus `--permission-mode bypassPermissions` for edit mode, and native `--json-schema`.
  - `GeminiAdapter`: Uses disposable copies plus `--approval-mode plan` for inspect mode, real target repositories plus `--approval-mode yolo` for edit mode, and prompt-based structured output repair.
  - `CursorAdapter`: Uses disposable copies plus `--plan` for inspect mode, real target repositories plus `--force --trust` for edit mode, and prompt-based structured output repair. Auth is inherited from `CURSOR_API_KEY` in the process environment.
- All heavy lifting happens through `tools/coder_tool.py` (the thin wrapper that combines worker + skill).

The Flow (`flow.py`) only knows about stages, state, and the abstract tool. It never imports concrete adapters directly in normal operation.

### 3. Optional Crew Coordination Modes
- Each optional Crew (`plan_crew.py`, `do_work_crew.py`, `review_crew.py`) supports two `process` values via `worker.yaml`'s `crew.process` key: `"sequential"` (default) and `"hierarchical"`.
- **Sequential + delegation**: tasks keep their fixed `agent=` assignment and read prior task output via `Task.context=[...]`. Setting `crew.delegation.enabled: true` additionally gives that crew's coordinator/decision agent `allow_delegation=True` (CrewAI's `DelegateWorkTool` / `AskQuestionTool`) so it can pull in a specialist mid-task instead of only reading a frozen context summary.
- **Hierarchical + manager**: `crew.process: "hierarchical"` builds the Crew with `Process.hierarchical` and an auto-created manager (`manager_llm`, sourced from `crew.manager.llm`, falling back to the crew's own `llm` block). Tasks are built **without** a fixed `agent=` so the manager actually decides who runs each task at runtime. Delegation reliability depends heavily on using a capable tool-calling model for `manager.llm` — small/local models are known to mis-format delegation tool calls or have the manager skip delegation entirely.
- Each crew module exposes a `build_*_crew(...)` function (e.g. `build_review_crew`) that constructs the `Crew`/`Agent`/`Task` graph without invoking `.kickoff()`, so offline tests can assert on `crew.process`, `crew.manager_llm`, and per-agent `allow_delegation` without a live LLM. The corresponding `run_*_crew(...)` function calls the builder and then kicks off the crew.

## Configuration (Edit These First)

**Never** change Python code to alter behavior if it can be achieved by editing the YAML.

- `config/skills.yaml` — Maps each stage to the agent-skill that supplies the operating procedure.
- `config/worker.yaml` — Controls per-stage worker (`codex` | `grok` | `claude` | `gemini` | `cursor`), model, sandbox mode, timeouts, optional crew stages (including `process: sequential|hierarchical`, `delegation.enabled`, and `manager.llm`), and human-in-the-loop flags.

At startup the system prints a clear table of the resolved mapping. Always verify this table when debugging "why is X using Y?".

Human-in-the-loop is an opt-in checkpoint system. It is disabled by default in `worker.yaml`, supports `before_plan`, `before_do_work`, `before_review`, `after_review`, and `before_finalize`, can optionally capture one-line operator instructions plus persist an approval audit trail, supports opt-in advanced actions (`do_work -> review/replan/target-tasks`, `review -> replan/rerun-review/target-tasks/force revise/force pass`, `finalize -> skip/rerun-review/revise/replan/target-tasks`) plus stage- and gate-scoped action allowlists, aborted runs can be resumed from saved state at any supported gate, and single runs can override default worker/model/timeout, stage skill/worker/model/timeout, HITL settings, HITL action allowlists, and runtime stage extras from the CLI.

The `human_feedback.mode` key selects `"static"` (default; each gate's boolean decides) or `"conditional"` (the run stays autonomous and prompts only when a deterministic, state-derived trigger fires). Conditional gating lives behind one seam, `hitl_policy.should_prompt()`; the trigger→gate mapping is hardcoded, legacy gate booleans are ignored under conditional mode (gates without a trigger go silent, which `doctor` warns about), and a fired trigger persists a typed `TriggerReason` on the audit entry. Phase 0 triggers: `repeated_task_failure` (`before_do_work`) and `approaching_max_revisions` (`after_review`). See `docs/adr/0003-hitl-policy-seam.md`.

## Development Commands

Use `uv` (this project is not a poetry or pip-tools project).

```bash
# Install
uv sync --all-extras

# Run the full offline test suite (the important command)
uv run pytest -m offline

# Run with coverage
uv run pytest -m offline --cov=src

# Execute the Flow against a target repo (see README)
uv run python -m crewai_headless_flow \
  --request "..." \
  --target-repo /path/to/target

# Equivalent explicit run command
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /path/to/target \
  --config-dir config

# Detect-only environment/config checks; no model prompts or adapter runs
uv run python -m crewai_headless_flow doctor --config-dir config
uv run python -m crewai_headless_flow doctor --target-repo /path/to/target --format json

# Read-only target-repo readiness check
uv run python -m crewai_headless_flow preflight --target-repo /path/to/target

# File-drop queue (Phase 3): enqueue a request, drain the queue, list run history
uv run python -m crewai_headless_flow enqueue --request "..." --target-repo /path/to/target --queue-dir ./queue
uv run python -m crewai_headless_flow serve --queue-dir ./queue --once
uv run python -m crewai_headless_flow runs --runs-dir ./runs

# Linting & types (run before committing non-trivial work)
uv run ruff check .
uv run ruff format --check .
uv run mypy src
git diff --check
```

The old `main.py` is a legacy M0 spike and is not the primary entrypoint.

## Testing Discipline (Non-Negotiable)

- The marker `offline` means **zero network and zero real CLI binaries**. All such tests must continue to pass without any external services.
- `live_codex`, `live_grok`, `live_claude`, `live_gemini`, and `live_cursor` markers exist for optional real-CLI smoke tests. They are guarded by environment variables and are **not** run in CI.
- When adding new functionality, prefer adding strong offline tests (mocks for workers, fixtures for state, etc.) over live tests.
- If a change cannot be made while keeping the offline suite green, the design is wrong.

## Safety & Sandbox Rules

- Inspect and review stages **must never** be allowed to mutate the target repository.
  - Codex: the adapter creates a disposable copy under `/tmp/codex-inspect-*` and additionally enforces `sandbox: read-only` inside it (defense in depth).
  - Grok: the adapter creates a disposable copy under `/tmp/grok-inspect-*` and deletes it afterward.
  - Claude: the adapter creates a disposable copy and runs with `--permission-mode dontAsk`.
  - Gemini: the adapter creates a disposable copy and runs with `--approval-mode plan`.
  - Cursor: the adapter creates a disposable copy and runs with `--plan`.
- Edit stages (`do_work`, and potentially others) run non-interactively in the real target repository using each adapter's edit-mode approval flags: Codex `--dangerously-bypass-approvals-and-sandbox`, Grok `--always-approve`, Claude `--permission-mode bypassPermissions`, Gemini `--approval-mode yolo`, and Cursor `--force --trust`.
- Worker-reported changed-file paths are untrusted input: `workspace_changes.apply_changed_files` rejects absolute paths, `..` traversal, and symlink escapes before touching disk. Keep any new mergeback/staging code behind the same guard.
- Operator-declared deny globs (`paths.deny` in `worker.yaml`) are enforced at every Flow-owned merge/diff boundary: parallel/isolated mergeback fails a denied task closed (nothing leaves the workspace copy), in-place edits get post-hoc restore, and delivery filters denied paths from the staged set. Deny config is deliberately file-only — never add a CLI flag that can weaken it. `do_work.isolation: copy` is the full-containment option for serial edits. See `docs/adr/0009-deny-paths-and-serial-isolation.md`.
- Git writes live in exactly two scoped places: `delivery.py` (opt-in branch/commit, and push/PR gated on the verification predicate; per-path staging, no `--force`/`reset`/`add -A`) and `paths_policy.restore_denied_paths` (scoped to `git checkout -- <denied paths>`). Adapters and the Flow's stage logic must stay git-write-free.
- Verification (`verify.commands`) and delivery are Flow-owned subprocesses — never route them through a worker adapter, and never let `mode: advisory` or a human force-pass weaken the "push/pr require the latest verification to have passed" predicate. See `docs/adr/0007-objective-verification-gate.md`.
- Never bypass the adapter layer to call the raw CLIs in new code.
- Auth (API keys, `gh` auth, etc.) is the user's responsibility via `.env` / keychain. Do not hardcode secrets.

## Working With Skills

- To preview a vendored refresh safely first: run `uv run python scripts/refresh_agent_skills.py --commit <full-sha> --dry-run`.
- To refresh the pinned vendored snapshot: rerun that command without `--dry-run`. This refreshes the current vendored skill set, updates `vendor/agent-skills/VENDOR_COMMIT`, and syncs `NOTICE` plus `DESIGN.md`.
- To add or swap a skill: use that refresh command with `--skill <skill-name>` to add a vendored directory and `--drop-skill <skill-name>` to remove a stale one from the final vendored set, then update `config/skills.yaml`.
- Skill content is parsed for the "Process", "When to Use", and main instructional sections. Keep vendored skills reasonably fresh but pinned (see `NOTICE` and `DESIGN.md`).
- Do not edit vendored skills in place unless you are also contributing upstream.

## Adding New Workers / Adapters

Implement the `HeadlessCoder` protocol from `workers/base.py` (the `run(...)` method and `CoderResult`).

Key responsibilities of an adapter:
- Respect the `mode` (`inspect` vs `edit`).
- Handle sandbox/approval differences correctly.
- Provide structured output when the underlying CLI supports it; all workers should still participate in the shared validation/repair loop so review decisions stay consistent.
- Never mutate the caller's repository during inspect mode.
- Accept a `binary=` kwarg (default = the CLI's name) so operators can point the worker at a specific executable via the `workers:` config block.

Registration is one entry: add a `WorkerSpec` (adapter class, default binary, doctor help command, required flags) to `WORKER_SPECS` in `workers/__init__.py` and export the adapter in `__all__`. The Flow's `WORKER_ADAPTERS` and all of doctor's worker dicts are derived from that table. Then add appropriate offline tests.

## Repository State & Hygiene (as of 2026-06-01)

- Full 8-milestone implementation complete and published.
- Default branch: `main` (migrated from `master`).
- License: MIT (2026, rmerk).
- Hermetic CI: `.github/workflows/ci.yml` uses locked `uv sync`, smokes both source-tree CLI entrypoints (`python -m crewai_headless_flow` and `crewai-headless-flow`), validates `preflight`, builds the wheel, checks bundled wheel contents, installs the built wheel into a clean venv, smokes both packaged entrypoints, then runs `ruff check`, `ruff format --check`, `mypy src`, `pytest -m offline`, and `git diff --check`.
- GitHub topics added for discoverability.
- Working tree is expected to stay clean.

## Contribution Expectations

- Small, reviewable changes preferred.
- Update documentation (README, DESIGN.md, or this file) when behavior or extension points change.
- Run the full offline test suite before proposing changes that affect the Flow, workers, or skill loading.
- When extending for new use cases, prefer adding configuration options over forking the Flow topology.

## Future Work & Opportunities

These are the main directions worth considering next (as of June 2026). The official high-level list lives in `DESIGN.md`, but here is a more concrete, prioritized view:

### Official Future Directions (from DESIGN.md)
- Add more adapters beyond the current four workers
- Richer task decomposition and parallel execution inside `do_work`
- Better structured output and review-loop semantics (stronger JSON repair loops, schema tools, consistent validation behavior)
- Integration with actual CrewAI `Crew` objects for richer multi-agent stages

### Prioritized Opportunities

| Priority | Idea | Why | Effort |
|----------|------|-----|--------|
| **Done** | **Claude Code adapter** | Validates the "pluggable workers" architecture as a third opt-in production adapter. | Implemented |
| **Done** | **Strengthen structured output/review-loop semantics** | Validation, repair, and review decisions now share one contract across worker paths. | Implemented |
| **Done** | **Parallel task execution in `do_work`** | Independent tasks can now run through isolated workspaces with actual changed-file conflict detection. | Implemented |
| **Done** | **Improve CI & DX** | CI now uses locked installs, CLI smoke checks, lint, format, types, and offline tests. | Implemented |
| **Done** | **Expand runtime observability** | State files and debug reports now capture per-task attempts, isolated workspace/batch metadata, and crew round details. | Implemented |
| **Done** | **Gemini CLI adapter** | Validates the pluggable-worker architecture with a fourth production adapter and a second prompt-repair structured-output path. | Implemented |
| **Done** | **Cursor Agent CLI adapter** | Adds a fifth opt-in worker via `cursor agent --print` with plan/force inspect/edit normalization. | Implemented |
| **Highest** | **Extend HITL/runtime controls** | Extra gates, resumable stage inputs, review reruns, targeted task selection, first operator shortcuts, one-run skill overrides, and stage-scoped HITL action allowlists are in place; next value is deeper operator decisions plus any remaining runtime override gaps. | Medium |
| **Medium** | **Expand real-world examples/docs** | The architecture is broader now; runnable example coverage and operator docs will make it easier to adopt. | Low |

### Other Sensible Ideas
- Expand real-world examples and documentation
- Expand runtime observability beyond the current per-run stage snapshot/reporting
- Make the project easier to consume as a library (`pip install`)
- Continue extending HITL/runtime controls with deeper operator decisions and any remaining runtime override coverage gaps

When picking the next piece of work, deeper HITL/runtime controls and stronger example coverage are currently high-leverage moves.

## Related Documentation

- `README.md` — User-facing installation, demo, and configuration guide.
- `DESIGN.md` — Deep architectural rationale, adapter normalizations, and future directions.
- `CONTEXT.md` — Project glossary / ubiquitous language (domain terms like Gate, Trigger, Domain Model Integration).
- `docs/adr/` — Architecture Decision Records (numbered, append-only).
- `docs/architecture/` — Standing architecture analyses (e.g. `autonomy-gap-analysis.md`, the Phase 1–3 autonomy roadmap).
- `docs/plans/` — Design/implementation plans for in-flight and shipped features.
- `NOTICE` — Attribution for vendored agent-skills.

When in doubt, read the two YAML files and the adapter implementations before touching the Flow itself.
