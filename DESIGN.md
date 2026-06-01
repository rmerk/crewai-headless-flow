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

Two concrete adapters are shipped:

#### CodexAdapter (codex-cli 0.132.0)

- Uses `--sandbox read-only` for `mode=inspect`
- Uses `--sandbox workspace-write` + `--dangerously-bypass-approvals-and-sandbox` for `mode=edit`
- Uses `--output-schema` when a JSON schema is provided

#### GrokAdapter (grok 0.2.14)

**Critical normalizations** (because Grok's headless mode differs from Codex):

1. **Sandboxing / Safety**
   - `mode=edit`: passes `--always-approve`
   - `mode=inspect`: **never** passes `--always-approve`. Instead, the adapter creates a disposable copy (or git worktree) of the target repository under `/tmp/grok-inspect-*`, runs against the copy, and deletes it afterward.

2. **Structured Output**
   - Codex supports `--output-schema`.
   - Grok does not. The adapter therefore:
     - Injects the exact required JSON schema into the prompt ("Respond with **only** valid JSON matching this schema").
     - Requests `--output-format json`.
     - Parses the result with Pydantic.
     - Performs **one** repair retry with a corrective prompt if validation fails.

## Flag Reality vs Original Spec

The original design document assumed certain CLI flags that had changed by the time of implementation (June 2026):

| Tool   | Assumed in Spec                  | Reality (installed versions)                          | Adapter Behavior |
|--------|----------------------------------|-------------------------------------------------------|------------------|
| Codex  | `--ask-for-approval never`       | `--dangerously-bypass-approvals-and-sandbox`          | Used for edit mode |
| Grok   | No `--sandbox` flag              | Has `--sandbox <profile>` and `--worktree`            | Still uses disposable copy for inspect (safer, portable) |
| Both   | —                                | —                                                     | All edit calls are non-interactive; all inspect calls are read-only by construction |

These differences are explicitly normalized inside the adapters so the rest of the system (Flow, config, skills) does not need to care.

## Why This Architecture?

- **Separation of concerns**: The Flow only knows about stages and state. Skills provide methodology. Workers provide execution capability.
- **Reusability**: Changing the "brain" (which skill) or the "hands" (which CLI) requires only YAML edits.
- **Safety & Cost Control**: Inspect stages can never accidentally mutate the user's repository. All heavy work is opt-in and can be mocked for free offline testing.
- **Observability**: `print_stage_mapping()` makes the current wiring completely transparent at startup.

## Optional Review Crew

The `review` stage can optionally run a sequential CrewAI `Crew` before the Flow router makes its pass/revise decision. This keeps the Flow responsible for state and routing while letting specialized review agents collect evidence, check correctness, evaluate test coverage, and merge findings into one structured decision.

The Review Crew is disabled by default in `config/worker.yaml`. When enabled, it exposes only a custom inspect-mode tool backed by the configured review worker, so the read-only review invariant remains unchanged.

## CLI Automation Caveats

This project deliberately uses **subprocess + CLI automation**, not native SDKs. This has consequences:

- We must maintain knowledge of each CLI's flags, approval models, and output formats.
- Sandbox/approval behavior is the responsibility of the adapter author.
- Structured output is best-effort when the tool does not provide a schema mechanism (hence the Grok repair retry).
- Auth is left entirely to the user's environment (`.env`, keychain, etc.).

These trade-offs were accepted in exchange for zero dependency on any vendor's Python SDK and maximum flexibility to swap in future headless agents.

## Future Directions

See `AGENTS.md` → "Future Work & Opportunities" for a more detailed and prioritized view.

High-level directions:
- Add more adapters (e.g. Claude Code headless, Gemini CLI, etc.)
- Richer task decomposition and parallel execution inside `do_work`
- Better structured output extraction (JSON repair loops, schema enforcement tools)
- Expand CrewAI `Crew` usage beyond the optional Review Crew into planning or implementation stages

The two highest-leverage near-term moves currently appear to be:
1. Implementing a Claude Code adapter
2. Activating and hardening the existing Human-in-the-Loop (HITL) wiring (already partially built in `config/worker.yaml`, `FlowConfig`, and `test_hitl.py`)
