# Topology cutover uses FlowDefinition + `call: code`, not agent/crew YAML stages

## Status
Accepted.

## Context

CrewAI 1.15.2 runs decorator Flows by projecting them into `crewai.flow/v1` `FlowDefinition` objects. The schema supports `call: code` (`module:qualname`), `call: script`, `call: tool`, `call: agent`, `call: crew`, and `call: expression`, plus method-level `human_feedback`. Official authoring templates still emphasize agent/crew/expression and under-document `call: code`.

This repository’s value is Flow-owned safety: inspect-mode disposable copies, `paths.deny`, the verify gate, delivery predicates, pluggable headless adapters, and a HITL model (`hitl_policy`, allowlisted actions, abort/resume) that does not match CrewAI’s stock `human_feedback.emit` + LLM collapse path.

Research: `docs/research/2026-07-14-flowdefinition-cutover.md`.

## Decision

Adopt a **hybrid cutover**:

1. **Long-term end state:** author the canonical topology in a config-pack `flow.yaml` (`schema: crewai.flow/v1`); keep every safety-critical stage body in Python behind `call: code` import refs.
2. **Migration:** strangler — class Flow remains source of truth through extraction and a YAML twin with kickoff equivalence tests; flip the CLI/library entrypoint only after those gates; then retire decorator topology wiring.
3. **Config resolution:** crew-bundle style — missing `flow.yaml` falls back to the package/default definition; a present but invalid/incomplete file **fails closed**.
4. **Operator contract:** only the canonical plan→do_work→review→revise/pass/abort/fail graph is supported and offline-tested. Open custom DAGs are out of product scope for this program.
5. **Prefer `call: code` over `call: script`.** Script execution is unsandboxed and requires `CREWAI_ALLOW_FLOW_SCRIPT_EXECUTION=1` at build time; production paths must not rely on it for stage logic.

## Consequences

- Phase 0: offline projection snapshot of `CrewAIHeadlessFlow` plus declarative `call: code` / script-disabled smokes (`src/tests/test_flow_definition_projection.py`). No entrypoint change.
- Phase 1 (landed): stage bodies and stage-private helpers live under `src/crewai_headless_flow/stages/`; `CrewAIHeadlessFlow` keeps thin decorator wrappers so topology projection and offline tests stay unchanged. No HITL engine swap.
- Phase 2 (landed): canonical `config/flow.yaml` twin (`schema: crewai.flow/v1`, `do.ref` → `stages.*:execute_*`); `flow_topology.load_flow_definition` / `build_topology_twin_flow`; kickoff equivalence suite (`src/tests/test_flow_topology_equivalence.py`). CLI / `run_headless_flow` still use the decorator class.
- Phase 3 (separate work): flip library/CLI construction to the declarative definition; then retire decorator topology wiring.
- **Explicitly deferred:** replacing this repo’s HITL with CrewAI `human_feedback` (only reconsider with a custom `HumanFeedbackProvider`); migrating optional crews from CrewBase hybrid / `run_*_crew` to native `call: crew` declarations; expressing verify/deny/delivery in CEL or agents.

## Alternatives considered

- **Pure declarative agent/crew stages** — rejected; cannot enforce inspect copies, deny paths, verify, or delivery predicates.
- **Projection-only forever** — rejected as the end state; useful during Phase 0–2 but does not deliver an operator-visible topology source of truth.
- **Stock CrewAI human_feedback as HITL** — rejected for cutover scope; semantic mismatch with gate allowlists, conditional triggers, and delivery fail-closed rules (ADR-0007).
