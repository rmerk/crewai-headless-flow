# FlowDefinition cutover research (crewai 1.15.2)

**Date:** 2026-07-14  
**Repo:** crewai-headless-flow  
**Locked dependency:** `crewai==1.15.2` (`pyproject.toml`: `crewai>=1.15.2,<2.0.0`; `uv.lock` pins 1.15.2)

## 1. Executive verdict

A full cutover that rewrites this repo’s safety-critical stage logic as CrewAI-native `call: agent` / `call: crew` / `call: expression` YAML is the wrong direction: those action types cannot own inspect copies, deny-path enforcement, the verify gate, delivery predicates, or this repo’s HITL action model. The **best path** is a **topology-first declarative shell** — author `schema: crewai.flow/v1` for `start` / `listen` / `router` / `or` wiring, and keep every safety invariant behind **`call: code` import refs** into Python modules that remain offline-tested. Prefer `call: code` over `call: script` (script is opt-in, unsandboxed, and fails at `Flow.from_declaration` unless `CREWAI_ALLOW_FLOW_SCRIPT_EXECUTION=1`). Do **not** replace this repo’s HITL with CrewAI `human_feedback.emit` LLM-collapse routing without a custom `provider` that preserves gate allowlists, advanced actions, and `hitl_policy.should_prompt()`. Evidence also shows the Python `@start/@listen/@router` class is already projected into FlowDefinition with `call: code` method refs — the runtime already reads topology from FlowDefinition — so cutover is an authoring/extraction problem, not a missing engine feature.

**Recommended direction:** hybrid phased cutover — extract stage bodies → YAML topology + `call: code` → optional CrewAI HF provider adapter later; never move safety into agents/scripts.

## 2. Capability matrix (1.15.2 vs this repo’s needs)

| Repo need | FlowDefinition / runtime support in 1.15.2 | Fit |
|-----------|--------------------------------------------|-----|
| Topology: `plan → do_work → review → pass\|revise\|aborted\|failed` with `or_(plan, process_revision)` | `start`, `listen` (string or `{or:[…]}` / `{and:[…]}`), `router` + `emit` | **Yes** — matches current projection of `CrewAIHeadlessFlow` |
| Mutable rich `FlowState` | `state.type: pydantic` + `ref: module:QualName` (also `dict` / `json_schema`) | **Yes** — offline smoke bound `crewai_headless_flow.state:FlowState` (wrapped as `StateWithId`) |
| Stage bodies (plan/do_work/review/finalize/…) | `FlowCodeActionDefinition` (`call: code`, `ref`, optional `with`) | **Yes — primary seam** |
| Inline Python in YAML | `FlowScriptActionDefinition` (`call: script`, `code`, `language: python`) — schema says “not sandboxed”; runtime raises `FlowScriptExecutionDisabledError` unless env opt-in | **Usable but wrong default for this repo** |
| CrewAI `BaseTool` as a step | `FlowToolActionDefinition` (`call: tool`, `ref`, `with`) | **Partial** — only CrewAI tools; not a substitute for `HeadlessCoder` adapters |
| Optional Planning / Review / Do-work crews | `call: crew` with `from_declaration` or inline `with` (`CrewDefinition`) | **Partial** — format is CrewAI JSON/YAML crew declarations, not this repo’s `config/crews/*/agents.yaml` + Python tool injection |
| Router outcomes | Method `router: true` + `emit`; action must return one event string | **Yes** — same as `@router` |
| Inspect-mode disposable copies | Not a FlowDefinition feature | **Must stay in adapters / stage Python** (`DESIGN.md` / `AGENTS.md`) |
| `paths.deny` + restore | Not a FlowDefinition feature | **Must stay Flow-owned Python** (ADR-0009) |
| Verify gate + delivery predicates | Not a FlowDefinition feature | **Must stay Flow-owned Python** (ADR-0007) |
| Pluggable headless workers | Not a FlowDefinition feature | **Must stay behind `WORKER_SPECS` / `HeadlessCoderTool`** |
| This repo’s HITL (gates, allowlists, advanced actions, conditional triggers, abort/resume) | Method field `human_feedback` (`message`, `emit`, `llm`, `default_outcome`, `provider`, `learn`, …) | **Mismatch** — CrewAI HF is post-method feedback with optional LLM collapse to `emit`; this repo’s HITL is pre/post stage policy + structured actions (`hitl_policy`, `human_feedback_actions`) |
| Offline testability | `FlowDefinition.from_declaration` + `Flow.from_declaration` + `build_action` | **Yes** for topology/code/script parsing; stage safety stays in existing `-m offline` tests |
| Persistence / resume | `persist`, `config.checkpoint`; HF pending/resume tested upstream | **Possible later**; this repo already has RunStore / aborted checkpoints — do not dual-own without an ADR |

**Changelog bullets that matter (prefer these over marketing docs):**

- v1.14.8a: “Add script/code block action to FlowDefinition”; “Drive human feedback from the flow definition”; “Wire config and persistence from FlowDefinition into the runtime”; “Add `crewai run --definition` for declarative flows”
- v1.14.7a4 / a3: “Migrate `@listen/@router` runtime to read from FlowDefinition”; “Migrate `@start` to read from `FlowDefinition`”
- v1.15.0: “Add unified declarative flow loading”; “Add declarative Flow CLI support”; “Add crew actions”; “Add `each` composite action”

Sources: installed `crewai/flow/flow_definition.py`, `crewai/flow/runtime/_actions.py`, `crewai/flow/runtime/__init__.py`; docs changelog; GitHub tests listed in §7.

## 3. Docs vs schema gap

| Source | What it documents | Gap |
|--------|-------------------|-----|
| Installed schema `FlowAtomicActionDefinition` | Discriminated union: **`code` \| `tool` \| `crew` \| `agent` \| `expression` \| `script`**, plus composite **`each`** | Authoritative |
| CLI template `lib/cli/.../declarative_flow/AGENTS.md` (also shipped in `crewai_cli` 1.15.2) | “Allowed shapes” only **`crew` / `agent` / `expression`**; guidance says pick simplest among those three | **Omits `code`, `script`, `tool`, `each`, method `human_feedback`** |
| `FlowDefinition.skill()` in installed package | Documents script/tool/each/hitl; **`FlowCodeActionDefinition` is `hidden=True`** so “Code Action” section is absent from the skill markdown | Code exists in schema/runtime but is de-emphasized for agent authors |
| Official docs `https://docs.crewai.com/v1.15.2/` (`llms.txt`) | Strong coverage of decorator Flows + `@human_feedback`; **no dedicated FlowDefinition / `crewai.flow/v1` guide** found in the index | Docs lag schema; HF guide is decorator-centric |
| Template `flow.yaml` | Minimal `call: expression` starter | Does not demonstrate code/script |

**Correction to prior grill:** assuming “no code/script actions” was wrong for 1.15.2. Schema fields:

- `FlowCodeActionDefinition`: `call: "code"`, `ref`, `with` (alias)
- `FlowScriptActionDefinition`: `call: "script"`, `code`, `language: "python"` (default), description includes “This is not sandboxed.”
- `FlowToolActionDefinition`: `call: "tool"`, `ref`, `with`
- `FlowMethodDefinition.human_feedback`: `FlowHumanFeedbackDefinition`

Upstream tests assert JSON Schema descriptions for code/script/tool (`test_flow_definition.py`) and runtime script opt-in / HF-from-declaration (`test_flow_from_definition.py`).

## 4. Candidate strategies

### A. Topology YAML + `call: code` stage modules (recommended)

Author `crewai.flow/v1` YAML (or dict) for the seven methods already projected from `CrewAIHeadlessFlow` (`plan`, `do_work`, `review`, `process_revision`, `finalize`, `handle_aborted`, `handle_failed`), each `do: {call: code, ref: …}` (or shorthand `do: {ref: …}` — `call` defaults to `"code"`). Extract stage implementations into importable callables that expect the Flow instance (CodeAction binds refs via `__get__` onto the Flow).

| Pros | Cons |
|------|------|
| Preserves inspect/deny/verify/delivery/HITL/adapters in Python | Large extraction from ~4.4k-line `flow.py` |
| Matches how CrewAI already projects decorator Flows | CodeAction binding quirks (module fns become bound methods — design callables as `(self, …)` / flow methods) |
| Topology becomes reviewable/diffable config | YAML does not remove the need for Python ownership of safety |
| Enables twin: class DSL vs `Flow.from_declaration` equivalence tests (upstream pattern) | Entry/CLI changes required |

### B. Keep Python DSL; treat FlowDefinition as projection only

Continue `@start/@listen/@router` on `CrewAIHeadlessFlow`. Use `build_flow_definition(CrewAIHeadlessFlow)` for export, visualization, or “definition fidelity” tests. No operator-facing YAML topology.

| Pros | Cons |
|------|------|
| Lowest risk; changelog already migrated runtime to definitions | Does not deliver a “declarative cutover” |
| Zero safety-model churn | Misses config-level topology reuse |
| Offline suite stays as-is | Still stuck with monolithic `flow.py` |

### C. Pure declarative agents/crews for stages (reject for full cutover)

Replace plan/do_work/review with `call: agent` / `call: crew` YAML, expressions for routing.

| Pros | Cons |
|------|------|
| Matches CLI AGENTS.md happy path | **Cannot enforce** inspect copies, deny paths, verify gate, delivery predicates |
| Looks “native CrewAI” | Optional crews already exist under `config/crews/` with different loading/tool injection |
| | Review router returning free-form LLM text is unsafe vs `Literal["pass","revise","aborted"]` |
| | Breaks offline guarantees if stages need live LLMs |

### D. YAML topology + `call: script` for glue (not recommended as primary)

Use inline scripts for small state transforms; code for heavy stages.

| Pros | Cons |
|------|------|
| Scripts can mutate `state` and read `outputs`/`input` | Requires `CREWAI_ALLOW_FLOW_SCRIPT_EXECUTION=1` at **build** time |
| Upstream tests cover mutate/return | Explicitly unsandboxed; weaker reviewability than `ref` modules |
| | Easy to grow into an untestable YAML codebase |

### E. Adopt CrewAI `human_feedback` as the HITL engine (only with custom provider)

Map gates to methods with `human_feedback` + custom `provider` implementing `HumanFeedbackProvider`, possibly wrapping `escalation.py`.

| Pros | Cons |
|------|------|
| Aligns with CrewAI pending/resume events | Default path uses **LLM collapse** when `emit` is set — conflicts with deterministic action tokens |
| `provider` ref is first-class in schema | Conditional HITL (`hitl_policy.should_prompt`) is pre-method policy; CrewAI HF is post-method |
| | Risk of dual audit trails (`human_feedback_log` vs CrewAI `human_feedback_history`) |

## 5. Risks & open questions

1. **HITL semantic mismatch** — CrewAI `human_feedback.emit` + `llm` vs this repo’s allowlisted actions / conditional triggers / force-pass that must not unlock delivery (ADR-0007). Open: can a custom `provider` alone preserve fail-closed abort without LLM?
2. **CodeAction callable binding** — runtime does `handler.__get__(flow, type(flow))` for unbound callables. Free functions with `(value)` break when used with `with: {value: …}` (“multiple values for argument”). Stage refs should be written as flow-bound methods or `(self, …)` signatures. Verified offline locally; also implies extracted modules must document this contract.
3. **`call: script` footgun** — disabled by default; fails in `Flow.from_declaration` / action build; not sandboxed. Prefer code refs.
4. **`max_method_calls` default 100** (`FlowConfigDefinition`) — revise loops + parallel task rounds could approach the cap; must be configured intentionally if topology moves to definitions.
5. **Outputs vs state** — CLI AGENTS.md: action results do **not** auto-merge into `state`. This repo mutates `self.state` heavily inside methods; code actions must keep mutating Flow state explicitly (as today), not assume `outputs.*` persistence.
6. **Optional crews format gap** — `call: crew` / `from_declaration` expects CrewAI crew declarations; this repo’s `config/crews/` + `build_*_crew` inject tools from Python. Bridging needs an explicit design (keep Python crew runners behind `call: code`, or migrate crew packs).
7. **Auto memory on definition-built flows** — `Flow.from_declaration` auto-creates `Memory` unless suppressed; may surprise headless runs (telemetry/storage). Open: always pass memory-disable / `_skip_auto_memory` equivalent for this product.
8. **Docs lag** — operators/agents following only CLI `AGENTS.md` will not discover `call: code`. Internal cutover docs must cite installed schema, not the template alone.
9. **Equivalence of class vs declaration** — upstream `test_definition_human_feedback_equivalence` pattern exists; this repo would need the same for review routing + abort/fail before flipping the CLI default entrypoint.
10. **Monolith extraction risk** — moving methods without splitting verify/delivery/HITL helpers could produce circular imports or “flow object” god-callables that are harder to test than today’s class methods.

## 6. Recommended phased plan (evidence-backed; phase gates, no calendar dates)

### Phase 0 — Prove the seam (gate: offline smoke + projection green)

- Keep current decorator Flow as source of truth.
- Assert `build_flow_definition(CrewAIHeadlessFlow)` yields the expected method graph (`call: code` refs, `listen: {or: [plan, process_revision]}`, review `router: true`).
- Add a tiny offline test that `FlowDefinition.from_declaration` accepts a minimal topology with `call: code` and that script actions raise `FlowScriptExecutionDisabledError` without the env flag.
- **Gate:** projection snapshot test + local smoke documented below remain green under `pytest -m offline`.

### Phase 1 — Extract without changing entrypoint (gate: behavior parity)

- Split stage bodies / helpers out of `flow.py` into importable modules still invoked by class methods (thin wrappers).
- No operator YAML yet; no HITL engine swap.
- **Gate:** full `uv run pytest -m offline` green; no change to inspect/deny/verify/delivery invariants (ADR-0007/0009).

### Phase 2 — Declarative topology twin (gate: kickoff equivalence)

- Author `crewai.flow/v1` declaration whose methods `listen`/`router`/`emit` match the projection; each `do.ref` points at extracted callables.
- Run twin harness: class Flow vs `Flow.from_declaration` on fixture states (plan→pass, plan→revise loop, abort, verify-fail→revise) with mocked workers.
- CLI remains on class Flow.
- **Gate:** equivalence suite for routing + terminal states; definition path used only in tests.

### Phase 3 — Entrypoint switch (gate: doctor + offline + one live smoke opt-in)

- Switch library/CLI construction to `Flow.from_declaration` (or load YAML beside `worker.yaml`).
- Keep safety in Python behind code refs; document that CLI `AGENTS.md` is incomplete for this product.
- Configure `max_method_calls`, suppress unwanted auto-memory, never enable script execution in production paths unless explicitly required.
- **Gate:** doctor, offline suite, and existing live markers still optional/gated.

### Phase 4 — HITL / crews (optional; only if product value is clear)

- If adopting CrewAI HF: implement `HumanFeedbackProvider` wrapping escalation + action parsing; keep `hitl_policy.should_prompt` as a pre-step `call: code` gate that may skip HF.
- Keep optional crews behind `call: code` → `run_*_crew` unless/until crew declaration format can carry tool injection.
- **Gate:** ADR updating HITL ownership; offline tests for allowlists, conditional triggers, and delivery predicate unaffected by force-pass.

**Explicit non-goals for full cutover:** rewriting adapters as `call: tool`; expressing verify/delivery in CEL; relying on `call: script` for stage logic; using stock `human_feedback.emit` LLM routing for operator actions.

## Offline smoke (this research)

Ran locally against the project venv (`crewai 1.15.2`), no network. Automated under `pytest -m offline` in `src/tests/test_flow_definition_projection.py`:

| Check | Result |
|-------|--------|
| `FlowDefinition.from_declaration` parses `call: code` / `call: script` / `call: tool` / `human_feedback` | Pass |
| `human_feedback.emit` canonicalizes method `router=True` and clears method-level `emit` | Pass (schema validator) |
| `call: script` without env | `FlowScriptExecutionDisabledError` at `Flow.from_declaration` |
| `call: script` with `CREWAI_ALLOW_FLOW_SCRIPT_EXECUTION=1` | Kickoff mutates state and returns value |
| `call: code` with `(self, value=…)` ref + `with: {value: "${outputs.seed}"}` | Kickoff `3 → 6` |
| `listen: {or: [a, c]}` | Parses and runs |
| `state.type: pydantic` + `ref: crewai_headless_flow.state:FlowState` | Instantiates; expression reads `state.request` |
| `build_flow_definition(CrewAIHeadlessFlow)` | Seven methods, all `do.call == code`, expected listens/routers |

Note: `Flow.from_declaration(..., suppress_flow_events=True)` was used in smokes to avoid event-bus noise; classic decorator `Flow` kickoff still works independently.

## 7. Sources

### Installed package (venv site-packages for `crewai==1.15.2`)

- `crewai/flow/flow_definition.py` — `FlowDefinition`, action models (`FlowCodeActionDefinition`, `FlowScriptActionDefinition`, `FlowToolActionDefinition`, …), `FlowHumanFeedbackDefinition`, `FlowMethodDefinition.human_feedback`, `FlowDefinition.from_declaration`
- `crewai/flow/runtime/_actions.py` — `CodeAction`, `ScriptAction`, `ToolAction`, `CREWAI_ALLOW_FLOW_SCRIPT_EXECUTION`, `FlowScriptExecutionDisabledError`, `build_action`
- `crewai/flow/runtime/__init__.py` — `Flow.from_declaration`, definition-driven listen/router execution, `_run_human_feedback_step`, state model loading
- `crewai/flow/human_feedback.py` — `@human_feedback` decorator contract (LLM emit collapse, provider)
- `crewai/flow/skill.py` — `FlowCodeActionDefinition` `hidden=True`; skill skips for script/tool/hitl
- `crewai_cli/templates/declarative_flow/AGENTS.md` — incomplete action list (`crew`/`agent`/`expression` only)
- `crewai_cli/templates/declarative_flow/flow.yaml` — expression-only starter

### Official docs

- Changelog: https://docs.crewai.com/v1.15.2/en/changelog (v1.14.8a script/code/HF-from-definition; v1.14.7a* migrate decorators to FlowDefinition; v1.15.0 declarative loading/CLI)
- Flows concept: https://docs.crewai.com/v1.15.2/en/concepts/flows
- Human feedback (decorator guide): https://docs.crewai.com/v1.15.2/en/learn/human-feedback-in-flows.md
- Docs index: https://docs.crewai.com/llms.txt (no dedicated FlowDefinition page located for v1.15.2)

### GitHub first-party

- Template AGENTS.md: https://github.com/crewAIInc/crewAI/blob/main/lib/cli/src/crewai_cli/templates/declarative_flow/AGENTS.md
- Template flow.yaml: https://github.com/crewAIInc/crewAI/blob/main/lib/cli/src/crewai_cli/templates/declarative_flow/flow.yaml
- Schema/unit coverage: https://github.com/crewAIInc/crewAI/blob/main/lib/crewai/tests/test_flow_definition.py (`FlowCodeActionDefinition` / `FlowScriptActionDefinition` JSON Schema assertions; HF projection)
- Runtime coverage: https://github.com/crewAIInc/crewAI/blob/main/lib/crewai/tests/test_flow_from_definition.py (`test_script_action_requires_explicit_opt_in`, script mutate/return tests, `test_human_feedback_from_declaration_*`, equivalence/pending-resume)
- Related: `test_human_feedback_decorator.py`, `test_human_feedback_integration.py`, `test_async_human_feedback.py`

### This repo

- `AGENTS.md` — safety model, YAML-first config for skills/workers/crews, offline testing rule
- `DESIGN.md` — adapter normalizations, HITL, verify/delivery, deny paths
- `src/crewai_headless_flow/flow.py` — `@start plan`, `@listen(or_(plan, process_revision)) do_work`, `@router review`, revise/finalize/abort/fail
- `docs/adr/0003-hitl-policy-seam.md`, `0007-objective-verification-gate.md`, `0009-deny-paths-and-serial-isolation.md`
- `config/crews/` — optional crew agent/task YAML (distinct from FlowDefinition crew actions)
- `pyproject.toml` / `uv.lock` — crewai 1.15.2 pin
