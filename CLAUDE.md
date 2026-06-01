# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**AGENTS.md at the repo root holds the authoritative conventions.** Read it before making non-trivial changes. This file does not duplicate the core guidance.

## Python / UV Specifics

- This project uses **uv** exclusively. Do not introduce poetry, pip-tools, or another lockfile.
- Run commands with `uv run ...` (e.g. `uv run pytest -m offline`, `uv run ruff check .`).
- The virtual environment is managed by uv (`.venv`).

## Running the System

Primary entrypoint (see README for full examples):

```bash
uv run python -m crewai_headless_flow \
  --request "Add a feature..." \
  --target-repo /path/to/target-repo
```

The legacy `main.py` is an old M0 spike and is not used for real work.

## Key Files for Agents

- `config/skills.yaml` and `config/worker.yaml` — the two primary extension points. Prefer changing these over Python code.
- `src/crewai_headless_flow/workers/` — the pluggable headless coder adapters (Codex + Grok).
- `src/crewai_headless_flow/skills/loader.py` — how agent-skills are parsed and injected.
- `tests/` — strong emphasis on `-m offline` tests. Live CLI tests are rare and gated.

## Testing

Always prefer and protect the offline test suite:

```bash
uv run pytest -m offline
```

Live tests (`live_codex`, `live_grok`) require real CLI auth and are intentionally not part of the default / CI run.

## When Working on This Codebase

Follow the invariants in AGENTS.md, especially:
- Safety boundaries between inspect and edit modes.
- The Flow must remain ignorant of concrete worker implementations.
- All new core behavior must remain testable without network or real CLIs.

See AGENTS.md for the full project overview, architecture, safety rules, and contribution expectations.
