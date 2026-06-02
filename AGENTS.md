# AGENTS.md — crewai-headless-flow

Guidelines for AI agents (and humans using agents) working in this repository.

## Project Overview

**crewai-headless-flow** is a reusable, multi-agent CrewAI Flow that treats **Addy Osmani's agent-skills** as operating procedures ("the how") and delegates actual code editing, running, and testing to **pluggable headless coding CLIs** ("the hands").

- **Core idea**: The Flow provides the orchestration and state machine. Skills provide consistent methodology. Workers (CodexAdapter, GrokAdapter, ClaudeAdapter) provide the execution capability.
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
- Three production adapters:
  - `CodexAdapter`: Uses native `--sandbox` + `--output-schema`.
  - `GrokAdapter`: Uses disposable copies for safe inspect mode + prompt-based structured output + one repair retry.
  - `ClaudeAdapter`: Uses disposable copies plus `--permission-mode dontAsk` for inspect mode, real target repositories plus `--permission-mode bypassPermissions` for edit mode, and native `--json-schema`.
- All heavy lifting happens through `tools/coder_tool.py` (the thin wrapper that combines worker + skill).

The Flow (`flow.py`) only knows about stages, state, and the abstract tool. It never imports concrete adapters directly in normal operation.

## Configuration (Edit These First)

**Never** change Python code to alter behavior if it can be achieved by editing the YAML.

- `config/skills.yaml` — Maps each stage to the agent-skill that supplies the operating procedure.
- `config/worker.yaml` — Controls per-stage worker (`codex` | `grok` | `claude`), model, sandbox mode, timeouts, and human-in-the-loop flags.

At startup the system prints a clear table of the resolved mapping. Always verify this table when debugging "why is X using Y?".

Human-in-the-loop v1 is an opt-in approve/abort checkpoint system. It is disabled by default in `worker.yaml` and supports only `before_do_work` and `before_finalize`.

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

# Linting & types (run before committing non-trivial work)
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

The old `main.py` is a legacy M0 spike and is not the primary entrypoint.

## Testing Discipline (Non-Negotiable)

- The marker `offline` means **zero network and zero real CLI binaries**. All such tests must continue to pass without any external services.
- `live_codex` and `live_grok` markers exist for optional real-CLI smoke tests. They are guarded by environment variables and are **not** run in CI.
- When adding new functionality, prefer adding strong offline tests (mocks for workers, fixtures for state, etc.) over live tests.
- If a change cannot be made while keeping the offline suite green, the design is wrong.

## Safety & Sandbox Rules

- Inspect and review stages **must never** be allowed to mutate the target repository.
  - Codex: the adapter enforces `sandbox: read-only`.
  - Grok: the adapter creates a disposable copy under `/tmp/grok-inspect-*` and deletes it afterward.
  - Claude: the adapter creates a disposable copy and runs with `--permission-mode dontAsk`.
- Edit stages (`do_work`, and potentially others) run non-interactively in the real target repository using each adapter's edit-mode approval flags: Codex `--dangerously-bypass-approvals-and-sandbox`, Grok `--always-approve`, and Claude `--permission-mode bypassPermissions`.
- Never bypass the adapter layer to call the raw CLIs in new code.
- Auth (API keys, `gh` auth, etc.) is the user's responsibility via `.env` / keychain. Do not hardcode secrets.

## Working With Skills

- To add or swap a skill: vendor the new `SKILL.md` under `vendor/agent-skills/skills/`, then update `config/skills.yaml`.
- Skill content is parsed for the "Process", "When to Use", and main instructional sections. Keep vendored skills reasonably fresh but pinned (see `NOTICE` and `DESIGN.md`).
- Do not edit vendored skills in place unless you are also contributing upstream.

## Adding New Workers / Adapters

Implement the `HeadlessCoder` protocol from `workers/base.py` (the `run(...)` method and `CoderResult`).

Key responsibilities of an adapter:
- Respect the `mode` (`inspect` vs `edit`).
- Handle sandbox/approval differences correctly.
- Provide structured output when the underlying CLI supports it; fall back to prompt injection + repair retry when it does not.
- Never mutate the caller's repository during inspect mode.

After adding a worker, update `config.py` resolution, the worker factory in `flow.py`, and add appropriate offline tests.

## Repository State & Hygiene (as of 2026-06-01)

- Full 8-milestone implementation complete and published.
- Default branch: `main` (migrated from `master`).
- License: MIT (2026, rmerk).
- Basic hermetic CI: `.github/workflows/ci.yml` (only `pytest -m offline`).
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
- Add more adapters (Gemini CLI, etc.)
- Richer task decomposition and parallel execution inside `do_work`
- Better structured output and review-loop semantics (stronger JSON repair loops, schema tools, consistent validation behavior)
- Integration with actual CrewAI `Crew` objects for richer multi-agent stages

### Prioritized Opportunities

| Priority | Idea | Why | Effort |
|----------|------|-----|--------|
| **Done** | **Claude Code adapter** | Validates the "pluggable workers" architecture as a third opt-in production adapter. | Implemented |
| **Highest** | **Strengthen structured output/review-loop semantics** | Grok has a basic repair retry and Claude/Codex have native schema paths; make validation, retries, and review decisions more consistent across workers. | Medium |
| **Medium** | **Parallel task execution in `do_work`** | Allow the implementation stage to break work into parallel tasks. | Medium–High |
| **Medium** | **Improve CI & DX** | Add mypy/ruff to CI, better formatting gates, optional smoke jobs. | Low |
| **Longer** | Deeper CrewAI `Crew` integration inside stages | Move beyond simple Flow topology to real multi-agent Crews per stage. | High |

### Other Sensible Ideas
- Expand real-world examples and documentation
- Add runtime observability (which skill + worker + model is active per stage)
- Make the project easier to consume as a library (`pip install`)
- Establish a clean process for refreshing the vendored `agent-skills`
- Extend HITL beyond v1 with instruction injection, resume-from-abort, CLI/runtime overrides, extra gates, or a persisted approval audit log

When picking the next piece of work, stronger structured output/review-loop semantics and parallel task execution are currently high-leverage moves.

## Related Documentation

- `README.md` — User-facing installation, demo, and configuration guide.
- `DESIGN.md` — Deep architectural rationale, adapter normalizations, and future directions.
- `NOTICE` — Attribution for vendored agent-skills.

When in doubt, read the two YAML files and the adapter implementations before touching the Flow itself.
