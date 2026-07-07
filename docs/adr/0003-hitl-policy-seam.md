# Conditional HITL lives behind a single `hitl_policy.should_prompt()` seam

## Status
Accepted.

We are adding a `mode: "conditional"` for human-in-the-loop that keeps runs mostly autonomous and only prompts the operator when a deterministic, state-derived condition (a "trigger") fires — as opposed to today's static gates, where each gate always or never prompts for the whole run.

We considered inlining the trigger logic at each of the five `_maybe_ask_human` call sites in `flow.py`, or growing `human_feedback_actions.py`. We rejected both: the decision "should this gate prompt right now?" is a distinct concern from "how do we prompt and record the answer," and scattering it across the flow would make it untestable in isolation and easy to drift between gates.

Instead, all of it lives behind one seam:

```
should_prompt(gate, hf_config, state, context) -> GateDecision
```

in `src/crewai_headless_flow/hitl_policy.py`. `flow.py`'s checkpoints call this in place of the old inline `hf.get(gate, True)` lookup; everything downstream (prompt rendering, action menu, allowlist, recording) is untouched and still gate-keyed, not trigger-keyed. This is the Strategy/Policy pattern, and it mirrors LangGraph's "conditional edge" router-function precedent — the closest real-world analog for "decide whether a human-interrupt fires."

Consequences and constraints:
- **Pure and offline-testable.** `should_prompt` and every trigger evaluator are functions over `FlowState` + a small `GateContext`; no I/O, no adapters, no LLM. The whole policy is exercised by `-m offline` tests.
- **Trigger→gate mapping is hardcoded** in `hitl_policy._TRIGGERS` (the single source of truth pairing name → gate → evaluator), not a config field — every Phase 0 trigger has exactly one sensible gate.
- **Persisted reason types live in `state.py`.** `TriggerReason` and its typed details are carried on `HumanFeedbackEntry`, so the audit layer owns them and the dependency stays one-directional (`state ← hitl_policy`), avoiding a circular import.
- **"conditional" is deliberately not "adaptive."** Phase 0 is pure threshold arithmetic; "adaptive"/"judged" is reserved for any future LLM-in-the-loop trigger so the two are never conflated.

Phase 0 ships two triggers (`repeated_task_failure` at `before_do_work`, `approaching_max_revisions` at `after_review`). Later phases (parallel-conflict, sensitive-path, review-volume, LLM-judged) plug into the same seam by adding a registry entry and an evaluator. See `docs/plans/2026-07-06-conditional-hitl-phase-0.md`.
