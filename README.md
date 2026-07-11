# crewai-headless-flow

A reusable, multi-agent CrewAI Flow that uses **Addy Osmani's agent-skills** as operating procedures and delegates actual coding work to **pluggable headless coding CLIs** (Codex, Grok, Claude Code, Gemini CLI, and Cursor Agent CLI).

**CrewAI** = orchestrator / control plane  
**agent-skills** = the "how" (operating procedures)  
**Headless coders** (Codex `exec` / Grok `-p` / Claude Code `-p` / Gemini `--prompt` / Cursor `agent --print`) = the "hands" that edit, run, and test code in a target repository

The workflow is fully re-targetable (any repo + any request) and re-shapeable (swap skills or swap the coding worker per stage) **using only YAML**.

## Cost Model

- Default planning, implementation, review, and finalize work → configured headless worker CLIs
  - Codex (ChatGPT plan or OpenAI API)
  - Grok (XAI_API_KEY)
  - Claude Code (user-managed Claude CLI install/auth)
  - Gemini CLI (user-managed Gemini CLI install/auth)
  - Cursor Agent CLI (user-managed Cursor CLI install/auth via `CURSOR_API_KEY` or `cursor agent login`)
- Optional Planning Crew / Implementation Crew / Review Crew reasoning → **Local Ollama** by default
- All tests are fully mocked — development and CI cost **$0** and require **no network**.

## Features

- **Skills as procedures**: Real agent-skills (planning-and-task-breakdown, incremental-implementation, code-review-and-quality, doubt-driven-development, etc.) are injected into every prompt.
- **Pluggable workers**: One `HeadlessCoder` interface with five production adapters:
  - `CodexAdapter` — uses native `--sandbox` + `--output-schema`
  - `GrokAdapter` — uses disposable copies for safe inspect mode + prompt-based structured output + repair retry
  - `ClaudeAdapter` — uses disposable copies for safe inspect mode + native `--json-schema`
  - `GeminiAdapter` — uses disposable copies plus `--approval-mode plan` for inspect mode, `--approval-mode yolo` for edit mode, and prompt-guided structured output repair
  - `CursorAdapter` — uses disposable copies plus `--plan` for inspect mode, real target repository plus `--force --trust` for edit mode, and prompt-guided structured output repair
- **Per-stage configuration**: Choose `codex`, `grok`, `claude`, `gemini`, or `cursor` (and model) independently for `plan`, `do_work`, `review`, and `finalize`.
- **Worker-backed planning**: The default `plan` stage now uses the configured stage worker in read-only inspect mode, so plan-stage worker/model settings are real rather than cosmetic.
- **Optional Planning Crew**: The `plan` stage can run a config-gated CrewAI Crew that researches the repo through the configured plan worker and emits the same `PlanOutput` contract.
- **Optional Implementation Crew**: The `do_work` stage can run a config-gated CrewAI subflow per task, using inspect plus bounded edit-tool execution, optional task-local decomposition, and bounded self-correction before marking the task complete.
- **Optional Review Crew**: The `review` stage can run a config-gated CrewAI Crew for richer multi-agent review while still using read-only worker inspection.
- **Two selectable crew coordination modes** (per crew, opt-in, default unchanged): **sequential + delegation** keeps `Process.sequential` and its fixed task-to-agent pipeline, optionally giving the coordinator agent `allow_delegation=True` to ask a specialist mid-task; **hierarchical + manager** switches to `Process.hierarchical` with an auto-created manager (`manager_llm`) that dynamically routes unassigned tasks at runtime. See [Crew coordination modes](#crew-coordination-modes) below — hierarchical mode needs a capable tool-calling `manager.llm`.
- **Shared review contract**: Direct review and Review Crew both normalize to one `status/issues/summary` schema, with native schema enforcement for Codex/Claude and repair-guided best effort for Grok/Gemini/Cursor.
- **Shared structured-output repair loop**: All adapters now run one consistent post-execution JSON extraction/validation path when a schema is supplied, with a single repair retry if the first response does not validate.
- **Structured planning output**: The `plan` stage now emits typed task data that populates Flow state, while still rendering markdown for downstream implementation prompts.
- **Opt-in parallel `do_work`**: When enabled in `worker.yaml`, the Flow can execute independent planned tasks in parallel batches using isolated workspace copies, then merge back only non-overlapping actual file changes.
- **Dynamic parallel batch planning**: When static file hints would only allow a one-task batch, an optional inspect-mode planner can pick a larger next batch and add missing file hints before execution.
- **Crew-aware `do_work` safety**: When `do_work.parallel.enabled` is also true, crew-backed tasks run inside the same isolated workspace batch model and still fail closed on overlapping actual edits.
- **Revision replanning**: After review returns `revise`, an optional inspect-mode replanner can replace the full structured task graph before the next `do_work` round while preserving unchanged completed tasks when possible.
- **Execution-time replanning**: When a task fails inside `do_work`, an optional inspect-mode replanner can replace the remaining task graph and continue the same `do_work` round.
- **Cross-task success replanning**: When a successful task changes files assigned to other remaining tasks, an optional inspect-mode replanner can replace the remaining graph inside the same `do_work` round.
- **Ambiguous-success replanning**: When a task exits successfully but the evidence is weak (for example no changed files or no overlap with its planned files), an optional inspect-mode replanner can replace the remaining graph inside the same `do_work` round.
- **Task-aware revise loop**: Review can map findings back to planned tasks/files, so retries reopen affected tasks instead of replaying the entire implementation stage by default.
- **Fail-closed structured review**: Automated review is downgraded to `revise` whenever any structured task is still not `done`; only an explicit human `force-pass` override can accept the run anyway.
- **Persistent flow history**: Task outcomes, review decisions, and revision targeting are recorded in Flow state for later debugging and final reporting.
- **Task execution trace**: Persisted state and debug reports now include per-task attempts, orchestration path (`direct` vs `crew`), isolated-workspace/batch metadata, and crew round summaries.
- **Runtime snapshot reporting**: Persisted state and debug reports include the resolved per-stage skill/worker/model/flags used by that run, separating real runtime knobs from enforced declarations where applicable.
- **Safe by design**:
  - Edit stages are fully non-interactive
  - Inspect/review stages are read-only (Codex native sandbox or Grok/Claude/Gemini/Cursor disposable copy)
- **Bounded revise loop** with `max_revisions`, which now fails the run closed if review still cannot pass by the configured cap
- **Optional human-in-the-loop** (config-gated)
- **100% offline testable** (`pytest -m offline`)
- **Hermetic CI gates**: GitHub Actions uses locked installs, smokes both source-tree entrypoints (`python -m crewai_headless_flow` and `crewai-headless-flow`), validates `preflight`, builds the wheel, checks bundled wheel contents, installs the built wheel into a clean venv, smokes both packaged entrypoints, then runs `ruff check`, `ruff format --check`, `mypy src`, `pytest -m offline`, and `git diff --check`

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

Installed wheels expose both `python -m crewai_headless_flow` and the console
script `crewai-headless-flow`.

### CLI Dependencies for Live Runs

Install only the headless worker CLIs you configure; each CLI is only required when a stage uses that worker.

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

**Gemini CLI**
```bash
# Install and authenticate via Gemini CLI instructions
gemini --version
```

**Cursor Agent CLI**
```bash
# Install Cursor CLI and authenticate via CURSOR_API_KEY or `cursor agent login`
cursor --version
cursor agent --help
```

**Ollama (only needed for optional Planning Crew / Implementation Crew / Review Crew)**
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
Gemini CLI authentication is managed by the Gemini CLI install/auth flow or its configured environment.
Cursor Agent CLI authentication is inherited from the process environment (`CURSOR_API_KEY`) or from `cursor agent login`. This project does not read shell dotfiles or pass API keys on the command line.

## Running the Demo

### 1. Create a sample target repository

```bash
uv run python examples/create_sample_target.py /tmp/demo-target
```

For curated config packs and copy-paste commands for common lanes, see [docs/operator-playbook.md](docs/operator-playbook.md).

### Config Pack Quick Guide

Use `config/` when you want the default path. Use one of the example packs when you
want a narrower, precomposed lane without editing YAML:

| Config dir | Use when |
|---|---|
| `config/` | You want the default direct-worker path: Codex for plan/review/finalize and Grok for `do_work`. |
| `examples/configs/claude-do-work` | You want Claude Code on the edit stage with the rest of the flow unchanged. |
| `examples/configs/gemini-do-work` | You want Gemini CLI on the edit stage with the rest of the flow unchanged. |
| `examples/configs/cursor-do-work` | You want Cursor Agent CLI on all stages with a single model lane. |
| `examples/configs/plan-gate` | You want an operator checkpoint before repository-wide planning starts. |
| `examples/configs/planning-crew` | You want the optional planning Crew without changing implementation/review topology. |
| `examples/configs/implementation-crew` | You want task-local CrewAI implementation rounds and optional decomposition inside `do_work`. |
| `examples/configs/implementation-crew-parallel` | You want task-local CrewAI implementation rounds plus conservative isolated-workspace parallel batching. |
| `examples/configs/implementation-crew-parallel-planner` | You want task-local CrewAI implementation rounds plus planner-assisted isolated-workspace parallel batching. |
| `examples/configs/implementation-crew-parallel-replan` | You want task-local CrewAI implementation rounds plus planner-assisted parallel batching and execution-time replanning. |
| `examples/configs/review-crew` | You want the optional review Crew for richer multi-agent inspection. |
| `examples/configs/operator-review-gate` | You want a human decision only after automated review findings are available. |
| `examples/configs/review-rerun-review-gate` | You want the review checkpoint to allow only a read-only review rerun after automated findings. |
| `examples/configs/review-targeting-only-gate` | You want the review checkpoint to reopen exact structured tasks after automated findings without broader review-loop shortcuts. |
| `examples/configs/review-replan-only-gate` | You want the review checkpoint to force structured replanning after automated findings without broader review-loop shortcuts. |
| `examples/configs/review-force-revise-gate` | You want the review checkpoint to reopen work with explicit issue text after automated findings. |
| `examples/configs/review-force-pass-gate` | You want the review checkpoint to accept the current run after automated findings without broader review-loop shortcuts. |
| `examples/configs/do-work-targeting-gate` | You want the operator to run only selected structured tasks before the edit stage begins. |
| `examples/configs/do-work-replan-gate` | You want the operator to force a fresh structured replan before the edit stage begins. |
| `examples/configs/do-work-skip-to-review-gate` | You want the operator to bypass the edit stage and jump straight to read-only review. |
| `examples/configs/review-targeting-gate` | You want the operator to reopen exact tasks after automated review. |
| `examples/configs/review-targeting-before-gate` | You want the operator to target tasks before automated review runs. |
| `examples/configs/review-force-revise-before-gate` | You want the operator to force a revise outcome before automated review runs. |
| `examples/configs/review-force-pass-before-gate` | You want the operator to force a pass outcome before automated review runs. |
| `examples/configs/review-replan-before-gate` | You want the operator to force a fresh structured replan before automated review runs. |
| `examples/configs/review-replan-gate` | You want review-time `rerun-review` or `replan` without enabling broader advanced actions. |
| `examples/configs/parallel-do-work` | You want conservative isolated-workspace parallel execution without planner or replanning extras. |
| `examples/configs/parallel-planner` | You want parallel execution plus the read-only next-batch planner without execution-time replanning. |
| `examples/configs/finalize-rerun-review-gate` | You want the final checkpoint to allow only a read-only review rerun before docs/ADR. |
| `examples/configs/finalize-replan-gate` | You want the final checkpoint to reopen work through the structured replanner before docs/ADR. |
| `examples/configs/finalize-force-revise-gate` | You want the final checkpoint to reopen work with explicit issue text before docs/ADR. |
| `examples/configs/finalize-skip-gate` | You want the final checkpoint to allow only skipping docs/ADR output. |
| `examples/configs/finalize-targeting-gate` | You want the final checkpoint to reopen only selected structured tasks before docs/ADR. |
| `examples/configs/finalize-review-gate` | You want a single final checkpoint that can rerun review or reopen work before docs/ADR. |
| `examples/configs/guided-operator-loop` | You want prompts before edit work, after review, and before finalize in one guided operator path. |
| `examples/configs/conditional-hitl` | You want the flow mostly autonomous, prompting only when a deterministic trigger fires (a task keeps failing, or the revise loop nears its ceiling). |
| `examples/configs/parallel-replan` | You want parallel `do_work`, planner-assisted batching, and execution-time replanning. |

The operator playbook keeps copy-paste commands for each pack.

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

To inspect the resulting state or export the execution report:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --format json

uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --debug-report-file /tmp/flow-report.md

uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --state-file /tmp/flow-state.json
```

`--state-file` captures the full persisted state, including the effective config
directory, the resolved per-stage runtime configuration, resolved
human-feedback configuration, and per-task execution telemetry used for that
run.

To resume a flow that previously stopped at a human gate:

```bash
uv run python -m crewai_headless_flow run \
  --resume-state-file /tmp/flow-state.json
```

Resumed runs accept the same runtime override flags as fresh runs, so you can
resume with a different worker, HITL, or stage-extra configuration without
editing YAML.

If the saved state came from a non-default config pack, the resume path reuses
that saved config directory automatically. Pass `--config-dir` on resume only
when you intentionally want to switch to a different config source.

To override stage wiring for one run without editing YAML:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-skill do_work=test-driven-development \
  --override-worker do_work=claude \
  --override-model do_work=sonnet \
  --override-timeout do_work=450
```

To override the fallback defaults used by stages that do not set their own
worker/model/timeout:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-default-worker claude \
  --override-default-model sonnet \
  --override-default-timeout 450
```

`--max-revisions` applies to both fresh runs and resumed runs, and must be at least `1`.

### 2b. Use It As a Library

The installed package also exposes a small programmatic API for Python callers:

```python
from pathlib import Path

from crewai_headless_flow import (
    load_runtime_config,
    render_execution_report,
    run_headless_flow,
)

config = load_runtime_config(config_dir=Path("config"))
state = run_headless_flow(
    request="Add a subtract function and a corresponding test using TDD",
    target_repo="/tmp/demo-target",
    config=config,
)

print(state.status)
print(render_execution_report(state))
```

If you do not need runtime overrides, `load_config(Path("config"))` is the lower-level
direct config loader.

To override HITL settings for one run without editing YAML:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback enabled=true \
  --override-human-feedback before_plan=true \
  --override-human-feedback before_do_work=false \
  --override-human-feedback before_review=true \
  --override-human-feedback after_review=true \
  --override-human-feedback before_finalize=false \
  --override-human-feedback capture_instructions=true \
  --override-human-feedback advanced_actions=true
```

Unknown HITL override keys fail fast. Use `--override-human-feedback-action` for
stage- or gate-scoped action allowlists instead of `--override-human-feedback`.

To override stage- or gate-scoped HITL actions for one run without editing YAML:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action do_work=replan,skip-to-review,target-tasks \
  --override-human-feedback-action after_review=replan,rerun-review,target-tasks,force-revise,force-pass \
  --override-human-feedback-action finalize=skip-finalize,rerun-review
```

Use `TARGET=none` to clear a stage- or gate-scoped action allowlist inherited
from the selected config pack:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/review-targeting-gate \
  --override-human-feedback-action after_review=none
```

If you want only the focused pre-edit targeting shortcut without also exposing
`replan` or `skip-to-review`, use the gate-scoped form:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_do_work=target-tasks
```

If you want only the focused pre-edit replanning shortcut without also exposing
`skip-to-review` or `target-tasks`, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_do_work=replan
```

If you want only the pre-edit skip-to-review shortcut without also exposing
`replan` or `target-tasks`, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_do_work=skip-to-review
```

If you want only the focused pre-review replanning shortcut without also exposing
`force-pass`, `force-revise`, or targeted reopen controls, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_review=replan
```

If you want only the focused pre-review force-revise shortcut without also
exposing `replan`, `force-pass`, or targeted reopen controls, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_review=force-revise
```

If you want only the focused pre-review force-pass shortcut without also
exposing `replan`, `force-revise`, or targeted reopen controls, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_review=force-pass
```

If you want only the review-time rerun-review shortcut without also exposing
`replan`, `target-tasks`, `force-revise`, or `force-pass`, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action after_review=rerun-review
```

If you want only the review-time targeting shortcut without also exposing
`rerun-review`, `replan`, `force-revise`, or `force-pass`, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action after_review=target-tasks
```

If you want only the review-time replanning shortcut without also exposing
`rerun-review`, `target-tasks`, `force-revise`, or `force-pass`, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action after_review=replan
```

If you want only the review-time force-revise shortcut without also exposing
`rerun-review`, `replan`, `target-tasks`, or `force-pass`, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action after_review=force-revise
```

If you want only the review-time force-pass shortcut without also exposing
`rerun-review`, `replan`, `target-tasks`, or `force-revise`, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action after_review=force-pass
```

If you want only the final rerun-review shortcut without also exposing reopen or
skip-finalize controls, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_finalize=rerun-review
```

If you want only the final replanning shortcut without also exposing rerun-review,
targeted reopen controls, or skip-finalize, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_finalize=replan
```

If you want only the final force-revise shortcut without also exposing rerun-review,
replanning, or skip-finalize, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_finalize=force-revise
```

If you want only the final skip shortcut without also exposing rerun-review or
reopen-work controls, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_finalize=skip-finalize
```

If you want only the final targeting shortcut without also exposing rerun-review,
replan, or skip-finalize controls, use:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-human-feedback-action before_finalize=target-tasks
```

To override nested stage extras for one run without editing YAML:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-stage-extra do_work.crew.enabled=true \
  --override-stage-extra do_work.parallel.enabled=true \
  --override-stage-extra do_work.parallel.max_workers=4 \
  --override-stage-extra do_work.parallel.planner.enabled=true \
  --override-stage-extra do_work.replan.enabled=true \
  --override-stage-extra do_work.replan.on_execution_failure=true \
  --override-stage-extra do_work.replan.on_cross_task_change=true \
  --override-stage-extra do_work.replan.on_ambiguous_success=true \
  --override-stage-extra do_work.replan.max_execution_replans=1 \
  --override-stage-extra review.crew.enabled=true
```

Unknown stage-extra paths or incompatible stage-extra values fail fast.
The safety declaration fields are also strict: `do_work.always_approve` must stay
`true`, and `review.sandbox` must stay `"read-only"`, because those behaviors are
enforced by the adapters rather than exposed as open-ended runtime knobs.
Crew process is strict too: `plan.crew.process`, `do_work.crew.process`, and
`review.crew.process` must currently stay `"sequential"`.

Starting from `config/`, you can also recreate the simple worker and single-stage
crew example packs with a few focused overrides:

Claude `do_work` lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-worker do_work=claude \
  --override-model do_work=sonnet
```

Gemini `do_work` lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-worker do_work=gemini \
  --override-model do_work=gemini-2.5-pro
```

Cursor all-stages lane:

```bash
uv run python -m crewai_headless_flow doctor --config-dir examples/configs/cursor-do-work

uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --config-dir examples/configs/cursor-do-work
```

Planning Crew lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-stage-extra plan.crew.enabled=true
```

Review Crew lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-stage-extra review.crew.enabled=true
```

Starting from `config/`, the default `do_work` stage already carries the same
direct-worker and parallel defaults used by the plain parallel example packs,
so you can recreate those lanes with only the batching and replanning knobs:

Conservative parallel lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-stage-extra do_work.parallel.enabled=true \
  --override-stage-extra do_work.parallel.max_workers=4
```

Planner-assisted parallel lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-stage-extra do_work.parallel.enabled=true \
  --override-stage-extra do_work.parallel.max_workers=4 \
  --override-stage-extra do_work.parallel.planner.enabled=true
```

Full replan-recovery parallel lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-stage-extra do_work.parallel.enabled=true \
  --override-stage-extra do_work.parallel.max_workers=4 \
  --override-stage-extra do_work.parallel.planner.enabled=true \
  --override-stage-extra do_work.replan.enabled=true \
  --override-stage-extra do_work.replan.on_execution_failure=true \
  --override-stage-extra do_work.replan.on_cross_task_change=true \
  --override-stage-extra do_work.replan.on_ambiguous_success=true \
  --override-stage-extra do_work.replan.max_execution_replans=2
```

Starting from `config/`, the default `do_work` stage already carries the same
local-LLM crew defaults (`max_rounds: 2`, Ollama `llama3.2`, and edit-stage
worker wiring) used by the example implementation-crew packs, so you can
recreate those lanes with only the enablement knobs:

Crew-only implementation lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-stage-extra do_work.crew.enabled=true \
  --override-stage-extra do_work.crew.decomposition.enabled=true \
  --override-stage-extra do_work.crew.decomposition.max_subtasks=3
```

Conservative parallel crew lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-stage-extra do_work.crew.enabled=true \
  --override-stage-extra do_work.crew.decomposition.enabled=true \
  --override-stage-extra do_work.crew.decomposition.max_subtasks=3 \
  --override-stage-extra do_work.parallel.enabled=true \
  --override-stage-extra do_work.parallel.max_workers=4
```

Planner-assisted parallel crew lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-stage-extra do_work.crew.enabled=true \
  --override-stage-extra do_work.crew.decomposition.enabled=true \
  --override-stage-extra do_work.crew.decomposition.max_subtasks=3 \
  --override-stage-extra do_work.parallel.enabled=true \
  --override-stage-extra do_work.parallel.max_workers=4 \
  --override-stage-extra do_work.parallel.planner.enabled=true
```

Full replan-recovery crew lane:

```bash
uv run python -m crewai_headless_flow run \
  --request "..." \
  --target-repo /tmp/demo-target \
  --override-stage-extra do_work.crew.enabled=true \
  --override-stage-extra do_work.crew.decomposition.enabled=true \
  --override-stage-extra do_work.crew.decomposition.max_subtasks=3 \
  --override-stage-extra do_work.parallel.enabled=true \
  --override-stage-extra do_work.parallel.max_workers=4 \
  --override-stage-extra do_work.parallel.planner.enabled=true \
  --override-stage-extra do_work.replan.enabled=true \
  --override-stage-extra do_work.replan.on_execution_failure=true \
  --override-stage-extra do_work.replan.on_cross_task_change=true \
  --override-stage-extra do_work.replan.on_ambiguous_success=true \
  --override-stage-extra do_work.replan.max_execution_replans=2
```

### 3. Check readiness without running a model

`doctor` is detect-only: it checks config, referenced skills, configured CLIs,
required CLI flags, and optional target-repo preflight. When a crew-backed stage
is still configured for the default Ollama-style local LLM, it also checks
Ollama readiness. It does not send model prompts, consume auth, construct the
Flow, or run worker adapters.

Text mode now also prints the resolved per-stage runtime summary plus resolved
HITL settings, so one-run overrides are visible without switching to JSON.

```bash
uv run python -m crewai_headless_flow doctor --config-dir config
uv run python -m crewai_headless_flow doctor --target-repo /tmp/demo-target --format json
```

`doctor` accepts the same runtime override flags as `run`, so you can validate
the exact one-run config before execution:

```bash
uv run python -m crewai_headless_flow doctor \
  --config-dir config \
  --override-worker do_work=claude \
  --override-stage-extra review.crew.enabled=true
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
1. Preview the final vendored skill set with `uv run python scripts/refresh_agent_skills.py --commit <full-sha> --dry-run`
2. If you need a new vendored skill directory, add `--skill <skill-name>` to that command
3. If you want to remove a stale vendored skill directory at the same time, add `--drop-skill <skill-name>`
4. Re-run without `--dry-run` to write the vendored files
5. Vendor files land under `vendor/agent-skills/skills/`, update `vendor/agent-skills/VENDOR_COMMIT`, and sync `NOTICE` + `DESIGN.md`
6. Update `config/skills.yaml` to point a stage at the new skill name

Example:

```bash
uv run python scripts/refresh_agent_skills.py \
  --commit 6ce029897d2b794940325fc7148774a6ec51111c \
  --dry-run
```

To swap one vendored skill for another in a single step:

```bash
uv run python scripts/refresh_agent_skills.py \
  --commit 6ce029897d2b794940325fc7148774a6ec51111c \
  --skill test-driven-development \
  --drop-skill doubt-driven-development
```

### Switch a stage to another worker
Just edit one line in `config/worker.yaml`.

### Enable the Planning Crew
The richer Planning Crew is available but disabled by default so the normal plan
path stays on the configured stage worker:

```yaml
stages:
  plan:
    crew:
      enabled: true
      process: "sequential"
      llm:
        model: "ollama/llama3.2"
        base_url: "http://localhost:11434"
        temperature: 0.2
```

The Planning Crew still researches through an inspect-mode tool, so planning remains read-only.

### Enable the Implementation Crew
The first Implementation Crew slice is available but disabled by default. It
wraps each planned task in an inspect/edit/verify CrewAI subflow while keeping
the actual mutation inside the configured stage worker:

```yaml
stages:
  do_work:
    crew:
      enabled: true
      process: "sequential"
      max_rounds: 2
      decomposition:
        enabled: true
        max_subtasks: 4
      llm:
        model: "ollama/llama3.2"
        base_url: "http://localhost:11434"
        temperature: 0.2
```

When `do_work.parallel.enabled` is also true, crew-backed tasks still run through
isolated per-task workspaces and only merge back when their actual changed files
do not overlap.
When `replan.enabled` is also true, that same crew-backed parallel lane can
replace the remaining task graph inside the current round after execution
failures, cross-task file drift, or ambiguous success evidence.

By default, `max_rounds: 2` means the crew gets one self-correction pass after a
task-level `revise` decision before the Flow marks the task failed.
When `decomposition.enabled: true`, the crew first tries to split one planned
task into a few ordered execution slices, then runs the normal inspect/edit/verify
loop per slice. If the decomposition output is weak or malformed, the Flow
fails open back to the normal one-task crew path.

For runnable config packs that enable this lane, see
`examples/configs/implementation-crew`,
`examples/configs/implementation-crew-parallel`, or
`examples/configs/implementation-crew-parallel-planner`, or
`examples/configs/implementation-crew-parallel-replan`, plus the matching
commands in `docs/operator-playbook.md`.

### Enable Dynamic Parallel Batch Planning
When `do_work.parallel.enabled: true`, the Flow normally picks the next batch
with a conservative static selector. If missing file hints or a conservative
static choice would otherwise leave unused parallel capacity, you can enable an
inspect-mode planner:

```yaml
stages:
  do_work:
    parallel:
      enabled: true
      max_workers: 4
      planner:
        enabled: true
```

The planner can choose a larger ready batch and add likely file hints, but it
falls back to the static selector on weak plans such as overlapping or still
missing effective file hints. Real mergeback still fails closed on overlapping
actual changes.

### Enable Revision Replanning
When review returns `revise`, the Flow normally reopens targeted tasks inside
the existing task graph. To let the system replace that graph before the next
implementation round, enable the inspect-mode revision replanner:

```yaml
stages:
  do_work:
    replan:
      enabled: true
      on_execution_failure: true
      on_cross_task_change: true
      on_ambiguous_success: true
      max_execution_replans: 1
```

The replanner returns a full revised plan, can split or reorder remaining work,
preserves unchanged completed tasks conservatively when possible, and when
`on_execution_failure: true` it can recover inside the same `do_work` round
after a task-level failure. When `on_cross_task_change: true`, it can also
replan after a successful task spills into files assigned to other remaining
tasks. When `on_ambiguous_success: true`, it can also recover from successful
but weak/no-op execution evidence such as zero changed files or no overlap with
the task's planned file list.

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

### Crew coordination modes
Each optional Crew (Planning, Implementation, Review) supports two `process`
values, opt-in per crew via `worker.yaml`. Both stay off by default — existing
configs are unaffected.

**Sequential + delegation** (`process: "sequential"`, the default) keeps
today's fixed task-to-agent pipeline (`Task.context=[...]` chaining). Setting
`delegation.enabled: true` additionally gives that crew's coordinator/decision
agent `allow_delegation=True` (CrewAI's `DelegateWorkTool` / `AskQuestionTool`),
so it can ask a specialist agent a follow-up question mid-task instead of only
reading a frozen context summary:

```yaml
stages:
  review:
    crew:
      enabled: true
      process: "sequential"
      delegation:
        enabled: true
      llm:
        model: "ollama/llama3.2"
        base_url: "http://localhost:11434"
        temperature: 0.2
```

**Hierarchical + manager** (`process: "hierarchical"`) switches the crew to
`Process.hierarchical`. CrewAI auto-creates a manager agent from `manager.llm`
(falling back to the crew's own `llm` block when unset), and tasks are built
without a fixed `agent=` assignment so the manager actually decides who runs
each task at runtime instead of following a pre-assigned pipeline:

```yaml
stages:
  review:
    crew:
      enabled: true
      process: "hierarchical"
      manager:
        llm:
          model: "gpt-4o"   # use a strong tool-calling model here
          base_url: "https://api.openai.com/v1"
          temperature: 0.2
      llm:
        model: "ollama/llama3.2"
        base_url: "http://localhost:11434"
        temperature: 0.2
```

Prefer sequential + delegation for narrow "ask a specialist" cases where you
still want a predictable, reviewable pipeline. Prefer hierarchical when you
want the manager to dynamically route work across agents. Hierarchical mode's
reliability depends heavily on `manager.llm`: small/local models are known to
mis-format delegation tool calls or have the manager skip delegation and do
all the work itself, so use a capable tool-calling model (e.g. a GPT-4o class
model) for `manager.llm` in that mode.

### Enable Human-in-the-Loop
Human-in-the-Loop (HITL) checkpoints are available but disabled by default so normal runs remain non-interactive. Enable them in `config/worker.yaml`:

```yaml
human_feedback:
  enabled: true
  before_plan: false
  before_do_work: true
  before_review: false
  after_review: false
  before_finalize: true
  capture_instructions: true
  advanced_actions: false
  action_allowlist: {}
```

HITL supports five gates: `before_plan`, `before_do_work`, `before_review`, `after_review`, and `before_finalize`. The read-only `plan`, `before_review`, and `after_review` gates default to off for backward compatibility; the mutating `do_work` and `finalize` gates default to on once HITL itself is enabled.

Each gate shows the stage, worker, skill, target repository, and mutation risk, then asks `Proceed? [y/N]`. The default is no: empty input, EOF, Ctrl-C, `n`, or anything other than `y`/`yes` aborts the flow with `status: "aborted_by_human"` and records a structured aborted-checkpoint snapshot: stage, exact gate, saved checkpoint message, any saved `before_review` instructions needed for review reruns, and the saved stage input needed for resume.

When `capture_instructions: true`, an approved checkpoint also prompts for optional one-line operator guidance, injects that guidance into that gated stage's prompt, and persists the full approval/abort event in Flow state plus the execution report. For `after_review`, approved guidance on a `revise` outcome is carried into the next revise loop as an extra review note. A later `run --resume-state-file ...` invocation resumes from any supported gate (`plan`, `do_work`, `review`, or `finalize`). If the flow stopped at `review@after_review`, resume reopens that saved automated-review checkpoint directly from the persisted aborted-checkpoint snapshot, including the saved `before_review` guidance needed if the operator chooses `rerun-review`, instead of rerunning the review worker first. If the flow stopped at `finalize@before_finalize`, resume reopens that final checkpoint too; when a saved latest work summary exists, `rerun-review` remains available there without rerunning `do_work`.

When `advanced_actions: true`, stage-specific shortcuts become available:

- `do_work`: `review` skips the edit stage and routes directly to review with a synthetic work summary
- `do_work`: `replan` reruns the planning stage before edit mode with operator guidance; this action is only offered before any task execution has started
- `do_work`: `target-tasks` narrows the current structured execution round to exact task IDs (plus any unmet dependencies), then leaves the rest of the task graph pending for a later loop
- `review` (before or after automated review): `replan` forces the next revise loop through the structured revision replanner with operator guidance
- `review` (after automated review only): `rerun-review` reruns the read-only review stage with operator-supplied guidance before committing to `pass` or `revise`
- `review` (before or after automated review): `target-tasks` selects exact structured task IDs to reopen for the next revise loop without changing the task graph; it accepts comma-separated task IDs, ranges like `2-4`, `all`, `hinted`, or exact file selectors like `file:docs/readme.md`
- `review` (before or after automated review): `revise` force-routes into the revise loop with operator-supplied issue text
- `review` (before or after automated review): `pass` force-passes review
- `finalize`: `skip` completes the flow without running final documentation
- `finalize`: `revise` reopens the revise loop with operator-supplied issue text before any final docs are written
- `finalize`: `replan` reopens the revise loop through the structured revision replanner before any final docs are written
- `finalize`: `rerun-review` reruns the read-only review stage against the saved latest work summary before any final docs are written
- `finalize`: `target-tasks` reopens exact structured task IDs before finalize, using the same task-id/range/file selector syntax as review targeting

When `action_allowlist` is set, it becomes a stage- or gate-scoped subset override for advanced actions. Gate-scoped entries take precedence over stage-scoped entries for that same checkpoint, which lets you enable `after_review -> force-pass` without also exposing that shortcut at `before_review`.

These advanced actions stay opt-in so the default safety model remains explicit approve/abort only.

#### Conditional Human-in-the-Loop (autonomous unless a trigger fires)

By default HITL is **static**: each gate either always prompts or never does, for the whole run. Set `human_feedback.mode: "conditional"` to instead keep the run autonomous and prompt only when a deterministic, state-derived **trigger** fires. Phase 0 ships two triggers:

| Trigger | Gate | Fires when |
|---|---|---|
| `repeated_task_failure` | `before_do_work` | A not-yet-done task has failed `min_attempts - 1` consecutive times since its last success (default `min_attempts: 2` → after 1 prior failure) |
| `approaching_max_revisions` | `after_review` | `revisions >= max_revisions - within` (default `within: 1`) |

```yaml
human_feedback:
  enabled: true
  mode: "conditional"          # "static" (default) | "conditional"
  before_finalize: false       # see the silent-gate note below
  conditional:
    triggers:
      repeated_task_failure:
        enabled: true
        min_attempts: 2
      approaching_max_revisions:
        enabled: true
        within: 1
```

**Silent-gate consequence — read this before flipping `mode: conditional`.** Under conditional mode the five legacy gate booleans are **ignored entirely**. Gates with no Phase 0 trigger (`before_plan`, `before_review`, `before_finalize`) therefore go permanently silent regardless of their boolean value, until a future phase adds a trigger for them. Because `before_do_work` and `before_finalize` default to `true`, `doctor` warns when their now-dead boolean is left set under conditional mode. The trigger→gate mapping is fixed in code (not configurable); a trigger's config carries only `enabled` and its own thresholds. See `examples/configs/conditional-hitl`, and tune a threshold for one run with `--override-human-feedback conditional.triggers.repeated_task_failure.min_attempts=3`.

When a trigger fires, its reason is appended to the checkpoint message and persisted as a structured `trigger_reason` on the audit entry, so the log distinguishes "static gate fired" from exactly which trigger fired.

## Unattended Runs (autonomy Phase 1)

Phase 1 of `docs/architecture/autonomy-gap-analysis.md` makes a no-TTY run safe: it either completes on its own branch, parks resumably awaiting approval, or fails with a resumable checkpoint — it never crashes unrecoverably. The pieces:

### Run directories & crash resume

Every run gets an identity and a durable home under `--runs-dir` (default `./runs`; pass `--runs-dir none` to disable): `runs/<run_id>/state.json` and `debug_report.md` are checkpointed **at every state mutation** with atomic writes. A crashed run (killed process, machine reboot) resumes from its last checkpoint:

```bash
uv run crewai-headless-flow run --resume-state-file runs/<run_id>/state.json
```

This is the same flag used for human-aborted runs; crashed runs (`status: "running"` in the snapshot) are now accepted too. Interrupted `in_progress` tasks are reset to `pending`; completed tasks stay done. `--state-file`/`--debug-report-file` remain as optional extra copies.

### Escalation channels (HITL without a terminal)

`human_feedback.escalation.channel` decides how a fired gate reaches a human:

| Channel | Behavior |
|---|---|
| `stdin` (default) | Blocking terminal prompt — the pre-Phase-1 behavior. |
| `file` | Writes `pending_approval.json` into the run dir and **parks the run resumably**. Answer by adding an `"answer"` field (e.g. `"y"`) to the file and re-running with `--resume-state-file`; the replayed gate consumes the answer. |
| `command` | Runs your hook (`command: [argv...]`) with the request JSON on stdin and reads the answer from its stdout, honoring `timeout_seconds` and `on_timeout: abort\|proceed`. Plug Slack/email/webhook notification in here — the platform itself stays network-free. |

One-run override: `--override-human-feedback escalation.channel=file`.

### Git delivery

With `deliver.enabled: true` (or `--override-deliver enabled=true`), a completed run commits the flow's own changed files onto a fresh `flow/<run_id>` branch instead of leaving a dirty tree. Guardrails: never commits on the branch you were on, refuses `protected_branches`, stages only the flow's files per-path (pre-existing dirt stays yours, uncommitted), and never force-pushes. With `deliver.push: true` the branch is pushed to `deliver.remote` (default `origin`), and `deliver.pr: true` opens a PR via the `gh` CLI after a successful push — both require the latest verification round to have passed whenever `verify.commands` is configured (see below). A delivery/push/PR failure records an error but does not fail the run or demote the local commit. Note a delivered run ends checked out on the `flow/<run_id>` branch.

### Worker retry & fallback

Adapter infrastructure failures (`WorkerTimeout`, a CLI that fails to launch) no longer crash the process — they become ordinary task failures routed into the revise loop. Per stage you can add bounded retries and a fallback worker (fires **only** on infrastructure errors, never on a worker's non-zero exit):

```yaml
stages:
  do_work:
    retry: {max_attempts: 2, backoff_seconds: 5}
    fallback_worker: "claude"
```

One-run override: `--override-stage-extra do_work.retry.max_attempts=2`.

## Verification & Observability (autonomy Phase 2)

Phase 2 of `docs/architecture/autonomy-gap-analysis.md` makes "completed" mean something objective and every run diagnosable after the fact.

### Objective verification gate

Declare the commands that must pass before review can (`verify:` in `worker.yaml`):

```yaml
verify:
  commands: ["uv run pytest -q", "uv run ruff check ."]
  mode: gate      # gate | advisory
  timeout: 600    # per command, seconds
```

The Flow runs them in the target repo at the top of **every** review round (fail-fast, argv with no shell — wrap pipes in a script). Under `mode: gate` a failure skips the LLM review entirely and feeds the command output tails into the revise loop as concrete issues; `mode: advisory` appends the results to the review prompt as evidence instead. Either way, `deliver.push`/`deliver.pr` only ship when the latest verification round passed (empty `commands` = you opted out and own the risk; `doctor` warns if push/pr is enabled unverified). Results are recorded on `state.verification_runs` and in the debug report's `## Verification` section. One-run override: `--override-verify 'commands=["uv run pytest -q"]'` (`commands=[]` is refused — disabling the gate is a `worker.yaml` decision, not a per-run flag).

### JSONL event log

Alongside `state.json`/`debug_report.md`, every run with a run dir appends structured events to `runs/<run_id>/events.jsonl` — one JSON object per line with `{ts, run_id, revision, kind, ...}`. Kinds cover stage starts, task completion/failure, review decisions, replans, human feedback/aborts, verification, delivery, and run completion/failure. A resumed run continues the same file. Diagnostic narration also goes through Python `logging` (`crewai_headless_flow` logger) instead of bare prints, so library users can route or silence it.

### Deny paths & serial isolation

Keep workers out of files they must never touch (`paths:` in `worker.yaml`):

```yaml
paths:
  deny: ["*.env", ".github/workflows/*", "secrets/*"]
```

Globs are matched against every changed file at the Flow's merge/diff boundaries (`*` crosses `/` — broad by design). Denied changes never merge out of isolated workspaces (the task fails closed); in-place edits get post-hoc restore (tracked files via `git checkout --`, run-created files deleted; pre-existing untracked files are reported unrestorable — deliberately never deleted). Denied paths are always excluded from delivery. Deny config is file-only: no CLI flag can weaken it.

For full serial containment, `do_work.isolation: copy` (or `--override-stage-extra do_work.isolation=copy`) runs single-task and direct edits in a disposable workspace copy — a failed or denied edit leaves the target repo pristine.

### Configurable worker binaries

Point any worker at a specific executable (`workers:` in `worker.yaml`):

```yaml
workers:
  codex: {binary: "/opt/bin/codex-nightly"}
```

`doctor` probes the configured binary too. One-run override: `--override-worker-binary codex=/opt/bin/codex-nightly`.

## Domain Model Integration (OpenWiki pass-through)

If your target repository already uses [OpenWiki](https://github.com/langchain-ai/openwiki) to generate and maintain its own domain documentation, **the `cursor`, `claude`, `codex`, and `grok` workers pick up that context automatically — no `crewai-headless-flow` configuration needed.** OpenWiki self-registers by appending a pointer into the target repo's `AGENTS.md`/`CLAUDE.md`, and all four workers already read those files natively when invoked in that repo's working directory.

This is deliberately **documentation-only**: the Flow never invokes `openwiki` itself (staleness is OpenWiki's own CI loop to manage) and never parses the `openwiki/` directory's structure. There is no `worker.yaml` toggle and no CLI override — it either works because the worker natively reads the pointer file, or it doesn't apply.

| Worker | Status | Notes |
|---|---|---|
| `cursor` | **Confirmed** | Reads `AGENTS.md` natively, no config required. |
| `claude` | **Confirmed** | Reads `CLAUDE.md` natively; OpenWiki writes both `AGENTS.md` and `CLAUDE.md`. |
| `codex` | **Confirmed** | Empirically verified with a live `codex exec` run (real read-only invocation shape) — it read and quoted a marker string from a test `AGENTS.md`. |
| `grok` | **Confirmed** | Empirically verified via `grok inspect --json` — this project's actual `workers/grok.py` binary lists `AGENTS.md` under its discovered project instructions. |
| `gemini` | Known gap | Gemini CLI defaults to `GEMINI.md` only. OpenWiki does not write `GEMINI.md`. If you use `gemini` with an OpenWiki-enabled target repo, add `context.fileName` (including `"AGENTS.md"`) to that target repo's own `.gemini/settings.json` — this is a target-repo setting, not a `crewai-headless-flow` one. |

See `docs/plans/2026-07-06-domain-model-integration.md` for the full design history and per-worker verification evidence.

## Testing

```bash
# All tests (fully offline, no CLIs, no network)
uv run pytest -m offline

# With coverage, etc.
uv run pytest -m offline --cov=src

# Mirror the CI quality job locally
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -m offline --tb=short
git diff --check

# Optional live Claude smoke (requires authenticated Claude CLI)
RUN_LIVE_CLAUDE=1 uv run pytest -m live_claude

# Optional live Gemini smoke (requires authenticated Gemini CLI)
RUN_LIVE_GEMINI=1 uv run pytest -m live_gemini
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
│   ├── claude.py           # ClaudeAdapter (with disposable-copy safety)
│   └── gemini.py           # GeminiAdapter (with disposable-copy safety)
└── tools/coder_tool.py     # Skill injection wrapper

config/
├── skills.yaml
└── worker.yaml

examples/
├── create_sample_target.py
└── configs/
    ├── claude-do-work/
    ├── conditional-hitl/
    ├── do-work-replan-gate/
    ├── do-work-skip-to-review-gate/
    ├── do-work-targeting-gate/
    ├── finalize-force-revise-gate/
    ├── finalize-replan-gate/
    ├── finalize-rerun-review-gate/
    ├── finalize-skip-gate/
    ├── finalize-targeting-gate/
    ├── finalize-review-gate/
    ├── gemini-do-work/
    ├── guided-operator-loop/
    ├── implementation-crew/
    ├── implementation-crew-parallel/
    ├── implementation-crew-parallel-planner/
    ├── implementation-crew-parallel-replan/
    ├── operator-review-gate/
    ├── review-force-pass-gate/
    ├── review-force-pass-before-gate/
    ├── review-force-revise-gate/
    ├── review-force-revise-before-gate/
    ├── review-replan-only-gate/
    ├── review-rerun-review-gate/
    ├── review-targeting-only-gate/
    ├── plan-gate/
    ├── planning-crew/
    ├── review-crew/
    ├── review-replan-before-gate/
    ├── review-replan-gate/
    ├── review-targeting-before-gate/
    ├── review-targeting-gate/
    ├── parallel-do-work/
    ├── parallel-planner/
    └── parallel-replan/

vendor/agent-skills/        # Pinned snapshot (see NOTICE)
```

## Reusability Story

This project was built so that:

- Changing *what* procedure is followed = edit `skills.yaml`
- Changing *who* actually edits the code = edit `worker.yaml`
- Changing the target repo or request = command line / API
- Adding another worker = implement one more adapter

No changes to the Flow topology or core logic are required.

## License

See [NOTICE](NOTICE) for attribution and licensing of vendored components.

## Contributing

PRs that improve safety, offline guarantees, or documentation are especially welcome.
