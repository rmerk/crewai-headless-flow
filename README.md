# crewai-headless-flow

A reusable, multi-agent CrewAI Flow that uses **Addy Osmani's agent-skills** as operating procedures and delegates actual coding work to **pluggable headless coding CLIs** (Codex, Grok, and Claude Code).

**CrewAI** = orchestrator / control plane  
**agent-skills** = the "how" (operating procedures)  
**Headless coders** (Codex `exec` / Grok `-p` / Claude Code `-p`) = the "hands" that edit, run, and test code in a target repository

The workflow is fully re-targetable (any repo + any request) and re-shapeable (swap skills or swap the coding worker per stage) **using only YAML**.

## Cost Model

- Orchestration LLM (planning, routing, light reasoning) → **Local Ollama** ($0)
- The actual coding work → pluggable headless coder (**opt-in**)
  - Codex (ChatGPT plan or OpenAI API)
  - Grok (XAI_API_KEY)
  - Claude Code (user-managed Claude CLI install/auth)
- All tests are fully mocked — development and CI cost **$0** and require **no network**.

## Features

- **Skills as procedures**: Real agent-skills (planning-and-task-breakdown, incremental-implementation, code-review-and-quality, doubt-driven-development, etc.) are injected into every prompt.
- **Pluggable workers**: One `HeadlessCoder` interface with three production adapters:
  - `CodexAdapter` — uses native `--sandbox` + `--output-schema`
  - `GrokAdapter` — uses disposable copies for safe inspect mode + prompt-based structured output + repair retry
  - `ClaudeAdapter` — uses disposable copies for safe inspect mode + native `--json-schema`
- **Per-stage configuration**: Choose `codex`, `grok`, or `claude` (and model) independently for `plan`, `do_work`, `review`, and `finalize`.
- **Optional Review Crew**: The `review` stage can run a config-gated sequential CrewAI Crew for richer multi-agent review while still using read-only worker inspection.
- **Safe by design**:
  - Edit stages are fully non-interactive
  - Inspect/review stages are read-only (Codex native sandbox or Grok/Claude disposable copy)
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

### CLI Dependencies for Live Runs

Install only the headless worker CLIs you configure; Claude Code is required only when a stage uses `worker: "claude"`.

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

**Claude Code**
```bash
# Install and authenticate via Anthropic's Claude Code instructions
claude --version
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

Claude Code authentication is managed by the Claude CLI, keychain, or environment. This project does not require a Claude-specific `.env` value.

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

The explicit subcommand form is equivalent:

```bash
uv run python -m crewai_headless_flow run \
  --request "Add a subtract function and a corresponding test using TDD" \
  --target-repo /tmp/demo-target \
  --config-dir config
```

### 3. Check readiness without running a model

`doctor` is detect-only: it checks config, referenced skills, configured CLIs,
required CLI flags, Ollama metadata readiness, and optional target-repo
preflight. It does not send model prompts, consume auth, construct the Flow, or
run worker adapters.

```bash
uv run python -m crewai_headless_flow doctor --config-dir config
uv run python -m crewai_headless_flow doctor --target-repo /tmp/demo-target --format json
```

`preflight` is a read-only target-repo check. It fails missing paths, file paths,
and merge conflicts before any adapter can create or mutate the target. Dirty,
staged, unstaged, untracked, detached-HEAD, non-git, and missing-tooling states
are reported as warnings or fields.

```bash
uv run python -m crewai_headless_flow preflight --target-repo /tmp/demo-target
```

### 4. Switch the worker for any stage (YAML only)

Edit `config/worker.yaml`:

```yaml
stages:
  do_work:
    worker: "codex"        # was "grok"
```

To opt in to Claude Code for a stage:

```yaml
stages:
  do_work:
    worker: "claude"
    model: "sonnet"
    timeout: 300
```

Re-run the same command — no code changes required. The startup banner will show the new mapping.

## Configuration

The entire behavior is driven by two small YAML files:

- `config/skills.yaml` — which agent-skill provides the operating procedure for each stage
- `config/worker.yaml` — which headless coder (`codex`/`grok`/`claude`), model, and flags to use per stage

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

### Switch a stage to another worker
Just edit one line in `config/worker.yaml`.

### Enable the Review Crew
The richer Review Crew is available but disabled by default to preserve the v0.1 behavior:

```yaml
stages:
  review:
    crew:
      enabled: true
      process: "sequential"
      llm:
        model: "ollama/llama3.2"
        base_url: "http://localhost:11434"
        temperature: 0.2
```

The Review Crew still receives only an inspect-mode tool, so review remains read-only.

### Enable Human-in-the-Loop
Human-in-the-Loop (HITL) checkpoints are available but disabled by default so normal runs remain non-interactive. Enable them in `config/worker.yaml`:

```yaml
human_feedback:
  enabled: true
  before_do_work: true
  before_finalize: true
```

HITL v1 is approve/abort only. It pauses before the mutating `do_work` stage and before `finalize`, shows the stage, worker, skill, target repository, and mutation risk, then asks `Proceed? [y/N]`. The default is no: empty input, EOF, Ctrl-C, `n`, or anything other than `y`/`yes` aborts the flow with `status: "aborted_by_human"` and records the aborted stage.

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
├── __main__.py             # python -m entrypoint
├── cli.py                  # argparse CLI: run, doctor, preflight
├── diagnostics.py          # detect-only doctor + read-only preflight checks
├── flow.py                 # The main CrewAI Flow
├── state.py                # Pydantic persisted state
├── config.py               # YAML resolver + pretty printer
├── skills/loader.py        # Parses vendored agent-skills
├── workers/
│   ├── base.py             # HeadlessCoder protocol + results
│   ├── codex.py            # CodexAdapter
│   ├── grok.py             # GrokAdapter (with disposable-copy safety)
│   └── claude.py           # ClaudeAdapter (with disposable-copy safety)
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
- Adding another worker (Gemini CLI, etc.) = implement one more adapter

No changes to the Flow topology or core logic are required.

## License

See [NOTICE](NOTICE) for attribution and licensing of vendored components.

## Contributing

PRs that improve safety, offline guarantees, or documentation are especially welcome.
