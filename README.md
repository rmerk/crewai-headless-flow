# crewai-headless-flow

A reusable, multi-agent CrewAI Flow that uses **Addy Osmani's agent-skills** as operating procedures and delegates actual coding work to **pluggable headless coding CLIs** (Codex and Grok).

**CrewAI** = orchestrator / control plane  
**agent-skills** = the "how" (operating procedures)  
**Headless coders** (Codex `exec` / Grok `-p`) = the "hands" that edit, run, and test code in a target repository

The workflow is fully re-targetable (any repo + any request) and re-shapeable (swap skills or swap the coding worker per stage) **using only YAML**.

## Cost Model

- Orchestration LLM (planning, routing, light reasoning) → **Local Ollama** ($0)
- The actual coding work → pluggable headless coder (**opt-in**)
  - Codex (ChatGPT plan or OpenAI API)
  - Grok (XAI_API_KEY)
- All tests are fully mocked — development and CI cost **$0** and require **no network**.

## Features

- **Skills as procedures**: Real agent-skills (planning-and-task-breakdown, incremental-implementation, code-review-and-quality, doubt-driven-development, etc.) are injected into every prompt.
- **Pluggable workers**: One `HeadlessCoder` interface with two production adapters:
  - `CodexAdapter` — uses native `--sandbox` + `--output-schema`
  - `GrokAdapter` — uses disposable worktrees for safe inspect mode + prompt-based structured output + repair retry
- **Per-stage configuration**: Choose `codex` or `grok` (and model) independently for `plan`, `do_work`, `review`, and `finalize`.
- **Safe by design**:
  - Edit stages are fully non-interactive
  - Inspect/review stages are read-only (Codex native sandbox or Grok disposable worktree)
- **Bounded revise loop** with `max_revisions`
- **Optional human-in-the-loop** (config-gated)
- **100% offline testable** (`pytest -m offline`)

## Installation

```bash
# 1. Clone
git clone https://github.com/your-org/crewai-headless-flow.git
cd crewai-headless-flow

# 2. Install with uv (recommended)
uv sync --all-extras

# 3. Copy environment file
cp .env.example .env
```

### Required CLIs

**Codex**
```bash
# Install via your preferred method (Homebrew, etc.)
codex --version
```

**Grok CLI**
```bash
# Install via your preferred method
grok --version
```

**Ollama (orchestration LLM — free)**
```bash
ollama serve
ollama pull llama3.2     # or qwen2.5-coder:7b, etc.
```

## Authentication

Edit `.env`:

```env
# For Codex (either ChatGPT plan auth or OpenAI key)
# OPENAI_API_KEY=sk-...

# For Grok
XAI_API_KEY=xai-...
```

## Running the Demo

### 1. Create a sample target repository

```bash
python examples/create_sample_target.py /tmp/demo-target
```

### 2. Run with the default config (Grok for `do_work`, Codex elsewhere)

```bash
uv run python -m crewai_headless_flow \
  --request "Add a subtract function and a corresponding test using TDD" \
  --target-repo /tmp/demo-target
```

### 3. Switch the worker for any stage (YAML only)

Edit `config/worker.yaml`:

```yaml
stages:
  do_work:
    worker: "codex"        # was "grok"
```

Re-run the same command — no code changes required. The startup banner will show the new mapping.

## Configuration

The entire behavior is driven by two small YAML files:

- `config/skills.yaml` — which agent-skill provides the operating procedure for each stage
- `config/worker.yaml` — which headless coder (`codex`/`grok`), model, and flags to use per stage

At startup the project prints a clear table:

```
crewai-headless-flow — Resolved Stage Configuration
Stage        Skill                            Worker     Model
------------------------------------------------------------------------
plan         planning-and-task-breakdown      codex      (default)
do_work      incremental-implementation       grok       grok-3-latest
review       code-review-and-quality          codex      (default)
finalize     documentation-and-adrs           codex      (default)
```

## How to Extend

### Add or swap a skill
1. Vendor the new `SKILL.md` under `vendor/agent-skills/skills/`
2. Update `config/skills.yaml` to point a stage at the new skill name

### Switch a stage to the other worker
Just edit one line in `config/worker.yaml`.

## Testing

```bash
# All tests (fully offline, no CLIs, no network)
uv run pytest -m offline

# With coverage, etc.
uv run pytest -m offline --cov=src
```

## Project Structure

```
src/crewai_headless_flow/
├── flow.py                 # The main CrewAI Flow
├── state.py                # Pydantic persisted state
├── config.py               # YAML resolver + pretty printer
├── skills/loader.py        # Parses vendored agent-skills
├── workers/
│   ├── base.py             # HeadlessCoder protocol + results
│   ├── codex.py            # CodexAdapter
│   └── grok.py             # GrokAdapter (with worktree safety)
└── tools/coder_tool.py     # Skill injection wrapper

config/
├── skills.yaml
└── worker.yaml

vendor/agent-skills/        # Pinned snapshot (see NOTICE)
```

## Reusability Story

This project was built so that:

- Changing *what* procedure is followed = edit `skills.yaml`
- Changing *who* actually edits the code = edit `worker.yaml`
- Changing the target repo or request = command line / API
- Adding a third worker (Claude Code, etc.) = implement one more adapter

No changes to the Flow topology or core logic are required.

## License

See [NOTICE](NOTICE) for attribution and licensing of vendored components.

## Contributing

PRs that improve safety, offline guarantees, or documentation are especially welcome.
