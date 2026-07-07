# Design of crewai-headless-flow

## Two Pillars

### Pillar A: Skills as Operating Procedures

We vendor a pinned snapshot of [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills) at commit `6ce029897d2b794940325fc7148774a6ec51111c`.

Each stage of the workflow is associated with one (or more) of these skills via `config/skills.yaml`. The `SkillLoader` parses the Markdown, extracts the core procedural guidance (the "Process" / "When to Use" / main instructional sections), and injects it into the prompt sent to the headless coder.

This gives the coding agent a consistent, high-quality methodology instead of vague instructions.

### Pillar B: Pluggable Headless Coders

The actual file editing, command execution, and testing is delegated to external headless CLIs via a small `HeadlessCoder` protocol:

```python
def run(
    self,
    task: str,
    cwd: str | Path,
    mode: Literal["inspect", "edit"],
    schema: dict | None = None,
    ...
) -> CoderResult
```

Five concrete adapters are shipped:

#### CodexAdapter (codex-cli 0.132.0)

- Uses `--sandbox read-only` for `mode=inspect`
- Uses `--sandbox workspace-write` + `--dangerously-bypass-approvals-and-sandbox` for `mode=edit`
- Uses `--output-schema` when a JSON schema is provided

#### GrokAdapter (grok 0.2.14)

**Critical normalizations** (because Grok's headless mode differs from Codex):

1. **Sandboxing / Safety**
   - `mode=edit`: passes `--always-approve`
   - `mode=inspect`: **never** passes `--always-approve`. Instead, the adapter creates a disposable filesystem copy of the target repository under `/tmp/grok-inspect-*`, runs against the copy, and deletes it afterward.

2. **Structured Output**
   - Codex supports `--output-schema`.
   - Grok does not. The adapter therefore:
     - Injects the exact required JSON schema into the prompt ("Respond with **only** valid JSON matching this schema").
     - Requests `--output-format json`.
     - Performs **one** repair retry with a corrective prompt if validation fails.
   - All workers now share one lightweight post-run validation path:
     - extract candidate JSON objects from wrapped CLI output
     - validate against the supplied schema
     - canonicalize valid JSON for downstream normalization
     - perform one repair retry when the first response does not validate

The review stage now shares one contract across both execution paths:
- direct Flow review passes the same `status/issues/summary` schema into the worker
- Review Crew coordinator returns the same normalized decision shape
- parsing fails closed to `revise` so the revise loop stays conservative

#### ClaudeAdapter (Claude Code CLI)

- Uses `claude -p` with `--output-format json` for non-interactive execution
- Uses a disposable filesystem copy plus `--permission-mode dontAsk` for `mode=inspect`
- Uses the real target repository plus `--permission-mode bypassPermissions` for `mode=edit`
- Uses native `--json-schema` when a JSON schema is provided
- Passes model names through unchanged with `--model`

#### GeminiAdapter (Gemini CLI)

- Uses `gemini --prompt ... --output-format json` for non-interactive execution
- Uses a disposable filesystem copy plus `--approval-mode plan` for `mode=inspect`
- Uses the real target repository plus `--approval-mode yolo` for `mode=edit`
- Does not have native JSON schema flags, so it uses the shared prompt-guided repair loop
- Passes model names through unchanged with `--model`

#### CursorAdapter (Cursor Agent CLI)

- Uses `cursor agent --print --output-format json` for non-interactive execution
- Uses a disposable filesystem copy plus `--plan --trust` for `mode=inspect`
- Uses the real target repository plus `--force --trust --workspace` for `mode=edit`
- Does not have native JSON schema flags, so it uses the shared prompt-guided repair loop
- Passes model names through unchanged with `--model`
- Inherits `CURSOR_API_KEY` from the process environment; the adapter never reads shell dotfiles or passes API keys on the command line

## Flag Reality vs Original Spec

The original design document assumed certain CLI flags that had changed by the time of implementation (June 2026):

| Tool   | Assumed in Spec                  | Reality (installed versions)                          | Adapter Behavior |
|--------|----------------------------------|-------------------------------------------------------|------------------|
| Codex  | `--ask-for-approval never`       | `--dangerously-bypass-approvals-and-sandbox`          | Used for edit mode |
| Grok   | No `--sandbox` flag              | Has `--sandbox <profile>` and `--worktree`            | Still uses disposable copy for inspect (safer, portable) |
| Claude | Native SDK-style adapter         | Headless CLI via `claude -p` with permission modes    | Uses disposable copy + `dontAsk` for inspect; real repo + `bypassPermissions` for edit |
| Gemini | Headless CLI with approval modes | `gemini --prompt` with JSON output and approval modes | Uses disposable copy + `plan` for inspect; real repo + `yolo` for edit |
| Cursor | Headless Agent CLI | `cursor agent --print` with plan/force modes and JSON output | Uses disposable copy + `plan` for inspect; real repo + `force` for edit |
| All    | —                                | —                                                     | All edit calls are non-interactive; all inspect calls are read-only by construction |

These differences are explicitly normalized inside the adapters so the rest of the system (Flow, config, skills) does not need to care.

## Structured Planning Contract

The planning stage now emits a typed `PlanOutput` contract:
- `spec`: concise planning summary used later by finalize
- `tasks`: ordered task objects with acceptance criteria, verification, dependencies, likely files, and estimated scope

By default, the `plan` stage now runs through the configured stage worker in read-only `inspect` mode with the `PlanOutput` schema, so plan-stage worker/model settings in `worker.yaml` are first-class runtime behavior rather than metadata.

The Flow stores the validated plan as `TaskItem`s in persisted state, then renders the structured plan back into markdown for `do_work`. This keeps current prompts readable while also making the task graph available for conservative parallel execution and task-aware revision targeting.

The plan contract also fails closed when the returned "structured" plan is missing a spec, missing tasks, or contains invalid task ids.

## Optional Planning Crew

The `plan` stage can optionally run a sequential CrewAI `Crew` before the Flow proceeds to implementation. This uses the configured plan worker only through an inspect-mode tool, so the Planning Crew can research the target repository without mutating it.

The Planning Crew is disabled by default in `config/worker.yaml`. When enabled, it emits the same `PlanOutput` contract as the direct worker-backed planning path, so downstream Flow behavior stays unchanged.

## Optional Implementation Crew

The `do_work` stage can optionally run a CrewAI `Crew` around each planned task. This slice uses:
- an inspect-mode tool to gather task context
- an edit-mode tool backed by the configured `do_work` worker
- an inspect-mode verification pass
- a final pass/revise decision using the same `status/issues/summary` contract as review
- a bounded retry loop so a task-level `revise` can trigger another implementation round before the Flow gives up
- an optional task-local decomposition step that can split one planned task into a few ordered execution slices before edit-mode work begins

That keeps the real mutation inside the configured worker while still moving beyond simple one-shot task execution inside `do_work`.

The Implementation Crew is disabled by default in `config/worker.yaml`. When enabled, it can now participate in the same isolated-workspace parallel batch model as direct task execution: disjoint tasks can run concurrently in separate workspace copies, mergeback still fails closed on overlapping actual changes, and `max_rounds` bounds task-local self-correction before the Flow marks a task failed.

## Conservative Parallel `do_work`

The first parallel execution slice is intentionally narrow:
- disabled by default in `config/worker.yaml`
- only uses structured `state.tasks`
- only batches tasks whose dependencies are already satisfied
- only runs tasks together when their `files` lists are both present and disjoint
- runs each task in an isolated workspace copy of the target repo
- detects actual changed files from the workspace diff before mergeback
- merges successful tasks back only when their actual changed files do not overlap

If those rules do not hold, execution falls back to a smaller sequential batch instead of forcing unsafe concurrency.

To push past that ceiling conservatively, parallel execution can now optionally run a read-only next-batch planner when the static selector leaves unused batch capacity. That planner:
- uses inspect mode only
- reuses the configured coding worker with planning-oriented prompt guidance
- can choose a larger next batch from the ready frontier and add missing file hints
- still falls back to the static selector on malformed output or weak plans, such as overlapping or still-missing effective file hints
- never bypasses the actual changed-file overlap gate during mergeback

After review returns `revise`, the Flow can also optionally run a read-only revision replanner before the next `do_work` round. That replanner:
- returns a full revised structured plan, not a partial patch
- can split, merge, reorder, or add remaining tasks
- prefers keeping unchanged completed tasks stable when possible
- falls back to the existing targeted-revision path on malformed output or weak plans
- keeps the actual mutation step in normal `do_work` execution, not in the replanner

Inside `do_work` itself, the Flow can also optionally run a read-only execution replanner after a task-level failure. That replanner:
- uses the failed task, runtime error, observed changed files, and current task graph as evidence
- replaces the remaining structured plan when recovery needs a different graph
- preserves successful completed tasks when their definitions still match
- continues the same `do_work` round after replanning instead of forcing an immediate stage abort

## Task-Aware Revise Loop

Once `do_work` is task-driven, review-loop retries need task awareness too. The Flow now:
- asks review to optionally map issues to task IDs and likely files
- infers task mappings heuristically from issue text and file-path mentions when the model omits explicit hints
- stores per-task review notes and failure state
- reopens only targeted tasks by default
- conservatively reopens downstream done tasks when an upstream dependency must change

If review cannot map findings confidently, the Flow fails safe by reopening all completed tasks.

Automated review also fails closed against the task graph itself: if any structured task is still not `done`, a model-level `pass` is downgraded to `revise` and targeted back at the incomplete task IDs/files. That keeps routes like `skip-to-review` from drifting into finalize on a structurally incomplete run, while still allowing an explicit human `force-pass` override when an operator intentionally accepts the state.

The bounded revise loop also fails closed at its cap: once `max_revisions` is exhausted, the Flow marks the run `failed` instead of synthesizing a passing review outcome. That keeps the terminal state aligned with what actually happened and lets CLI callers surface a non-zero result.

The Flow also persists compact history entries for:
- task completion/failure outcomes
- review decisions and mapped targets
- revision targeting batches

That gives later stages a lightweight audit trail without depending on raw CLI transcripts.

## Why This Architecture?

- **Separation of concerns**: The Flow only knows about stages and state. Skills provide methodology. Workers provide execution capability.
- **Reusability**: Changing the "brain" (which skill) or the "hands" (which CLI) requires only YAML edits.
- **Safety & Cost Control**: Inspect stages can never accidentally mutate the user's repository. All heavy work is opt-in and can be mocked for free offline testing.
- **Observability**: Startup prints the resolved stage mapping, and persisted Flow state plus the execution report carry the resolved per-stage runtime configuration and resolved human-feedback configuration for later debugging and automation.

## Optional Review Crew

The `review` stage can optionally run a sequential CrewAI `Crew` before the Flow router makes its pass/revise decision. This keeps the Flow responsible for state and routing while letting specialized review agents collect evidence, check correctness, evaluate test coverage, and merge findings into one structured decision.

The Review Crew is disabled by default in `config/worker.yaml`. When enabled, it exposes only a custom inspect-mode tool backed by the configured review worker, so the read-only review invariant remains unchanged.

## Human-in-the-Loop

Human-in-the-Loop (HITL) is an opt-in checkpoint system configured through `config/worker.yaml`. It is disabled by default and supports `before_plan`, `before_do_work`, `before_review`, `after_review`, and `before_finalize`. The read-only `plan`/`review` gates default to off for backward compatibility, while the mutating `do_work`/`finalize` gates default to on once HITL itself is enabled.

When enabled, the Flow prints the stage name, mutation risk, configured worker, configured skill, target repository, and default-no behavior before asking `Proceed? [y/N]`. Any response other than an allowed action alias defaults closed: empty input, EOF, Ctrl-C, `n`, `no`, or unrecognized text marks the Flow state as `aborted_by_human` and records the aborted stage.

When `capture_instructions: true`, an approved checkpoint also prompts for optional operator instructions. Those instructions are injected into that gated stage's prompt and every prompted HITL decision is persisted in Flow state plus the execution report as an audit trail. On `after_review`, approved instructions for a `revise` result are appended as extra review notes before the next implementation loop.

Aborts are resumable from the CLI for any supported gate. `run --resume-state-file ...` reloads persisted state, restores any saved stage input needed by that gate, and continues from the aborted point.

When `advanced_actions: true`, the gate prompt also enables small operator shortcuts that intentionally bypass one stage boundary without broadening ambient authority:

- `do_work`: skip directly to `review`
- `do_work`: rerun `plan` before edit mode with operator-supplied replanning guidance; this is only offered before any task execution has started
- `do_work`: narrow the current structured execution round to exact task IDs (plus any unmet dependencies) before edit mode begins
- `review` (before or after automated review): force the next revise loop through structured replanning with operator-supplied guidance
- `review` (after automated review only): rerun the read-only review stage with operator-supplied guidance before committing to `pass` or `revise`
- `review` (after automated review only): select exact structured task IDs to reopen for the next revise loop without changing the current task graph
- `review` (before or after automated review): force a `revise` result with operator-supplied issue text
- `review` (before or after automated review): force a `pass` result
- `finalize`: complete the flow without generating final docs

`action_allowlist` can further narrow or selectively enable those shortcuts per stage or gate. For example, one run can allow only `after_review -> replan` without also enabling that same shortcut at `before_review`, `do_work -> review`, or `finalize -> skip`.

These stay opt-in so the default behavior remains explicit approve-or-abort only.

### Conditional gating (`mode: conditional`)

By default HITL is `mode: "static"`: each gate always or never prompts for the whole run. Under `mode: "conditional"` the Flow instead stays autonomous and prompts only when a deterministic, state-derived **trigger** fires. The decision "should this gate prompt right now?" is factored out of `flow.py` into a single seam — `hitl_policy.should_prompt(gate, hf_config, state, context) -> GateDecision` — which the five checkpoint call sites consult in place of the old inline gate-boolean lookup. Everything downstream (prompt rendering, action menu, allowlist, recording) is unchanged and still gate-keyed. This is a pure, offline-testable policy module; the trigger→gate mapping is hardcoded (each Phase 0 trigger has exactly one sensible gate), and a trigger's config carries only `enabled` plus its own thresholds. When a trigger fires, a typed `TriggerReason` (owned by `state.py`, carried on `HumanFeedbackEntry`) is appended to the prompt and persisted, distinguishing a static-gate prompt from which trigger fired. Under conditional mode the legacy gate booleans are ignored, so gates with no trigger go silent; `doctor` warns about that dead config. Phase 0 ships `repeated_task_failure` (`before_do_work`) and `approaching_max_revisions` (`after_review`). See `docs/adr/0003-hitl-policy-seam.md` and `docs/plans/2026-07-06-conditional-hitl-phase-0.md`.

Human-feedback behavior is also overridable per run from the CLI, so operators can enable/disable gates or instruction capture without editing `worker.yaml`. Nested conditional keys are reachable via dotted override paths, e.g. `--override-human-feedback conditional.triggers.repeated_task_failure.min_attempts=3`.

Stage worker/model/timeout defaults are also overridable per run from the CLI, and nested stage extras remain overridable as well. That makes plan-crew, do_work-crew, parallel-execution, review-crew, and similar experimental knobs adjustable without editing `worker.yaml`.

## CLI Automation Caveats

This project deliberately uses **subprocess + CLI automation**, not native SDKs. This has consequences:

- We must maintain knowledge of each CLI's flags, approval models, and output formats.
- Sandbox/approval behavior is the responsibility of the adapter author.
- Structured output is native for Codex and Claude when schemas are provided, but all workers still pass through the same validation/repair loop so downstream semantics stay consistent.
- Auth is left entirely to the user's environment (`.env`, keychain, etc.).
- Ollama is only required when an optional Planning, Implementation, or Review Crew stage is enabled.

These trade-offs were accepted in exchange for zero dependency on any vendor's Python SDK and maximum flexibility to swap in future headless agents.

## Future Directions

See `AGENTS.md` → "Future Work & Opportunities" for a more detailed and prioritized view.

High-level directions:
- Add more adapters beyond the current five workers
- Richer task decomposition and parallel execution inside `do_work`
- Better structured output and review-loop semantics (JSON repair loops, schema enforcement tools, consistent validation behavior)
- Expand CrewAI `Crew` usage beyond the current bounded Planning/Implementation/Review Crews into richer implementation orchestration
- Extend HITL/runtime controls further with richer operator decisions and additional override types

The two highest-leverage near-term moves currently appear to be:
1. Extend HITL/runtime controls further with broader runtime override coverage and richer operator decisions
2. Expand real-world examples and operator-facing documentation now that the adapter surface is broader
