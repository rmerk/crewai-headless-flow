---
name: Conditional HITL Phase 0
overview: Add an opt-in "conditional" Human-in-the-Loop mode that keeps runs mostly autonomous by default and only prompts the operator when a deterministic, state-derived trigger condition fires, instead of today's always-on/always-off static gates.
todos:
  - id: hitl-policy-module
    content: Create hitl_policy.py with GateContext, TriggerReason (+ per-trigger detail dataclasses), GateDecision, and should_prompt()
    status: pending
  - id: config-schema
    content: Extend config.py's _validate_human_feedback with mode + conditional.triggers.* schema validation
    status: pending
  - id: state-schema
    content: Add HumanFeedbackEntry.trigger_reason field to state.py
    status: pending
  - id: flow-integration
    content: Wire should_prompt() into flow.py's five _maybe_ask_human call sites (lines 898, 1234, 2806, 2949, 3358) with per-gate GateContext construction; before_do_work's context must be built from self.state.tasks before execution_target_task_ids is resolved
    status: pending
  - id: cli-overrides
    content: Extend --override-human-feedback to support dotted nested paths for conditional.triggers.*
    status: pending
  - id: example-config
    content: Add examples/configs/conditional-hitl/ example pack
    status: pending
  - id: diagnostics
    content: Add doctor check warning when mode=conditional has zero triggers enabled, or when a gate with no Phase 0 trigger (before_plan/before_review/before_finalize) has its legacy boolean set to true and will now be dead config
    status: pending
  - id: docs
    content: Append Gate/Trigger entries to the existing CONTEXT.md (## Language section, already holds DMI's Domain Model Integration entries); update README.md, AGENTS.md, and DESIGN.md; record the hitl_policy.py seam as docs/adr/0003-hitl-policy-seam.md; add CONTEXT.md, docs/adr/, and docs/plans/ to AGENTS.md's Related Documentation list
    status: pending
  - id: tests
    content: Add offline tests for hitl_policy (including revision-spanning streaks, multi-task tie-break, off-by-one boundary), config validation, CLI overrides, flow integration, doctor check, and a resume/backward-compat test for HumanFeedbackEntry without trigger_reason
    status: pending
  - id: verify-recipes
    content: Re-run test_documented_*_override_recipes_match_example_packs after the schema change to confirm the DEFAULT_HUMAN_FEEDBACK merge really does make the new mode/conditional keys apply to all 34 example packs with zero edits, as assumed (do not just trust the assumption)
    status: pending
isProject: false
---


# Conditional HITL Phase 0

## Background

Today, `human_feedback` gates (`before_plan`, `before_do_work`, `before_review`, `after_review`, `before_finalize`) are static booleans resolved once at startup (`config/worker.yaml:157-166`, checked in `flow.py:589-676`'s `_maybe_ask_human`). A gate either always prompts or never does, for the whole run, regardless of what actually happens. This plan adds a second, opt-in mode where the flow decides per-gate, from real run state, whether operator input is actually warranted — resolving the design tree from the grilling session in this conversation.

## Scope (Phase 0 only)

Ship exactly two triggers, both pure arithmetic over `FlowState` with zero new instrumentation, fully offline-testable:

1. **`approaching_max_revisions`** — fires at `after_review` when `state.revisions >= state.max_revisions - within` (reads existing `state.py:157-158`, `state.py:267-268`).
2. **`repeated_task_failure`** — fires at `before_do_work` when a task's attempt count (from `state.task_executions`, computed via `_next_task_attempt()` at `flow.py:1402-1412`) meets or exceeds a configured threshold.

Later phases (`parallel_conflicts_detected`, `sensitive_paths_touched`, `review_issue_volume`, worker-confidence/LLM-judged triggers) are explicitly out of scope for this slice.

## Design decisions (resolved this session)

- **New module `src/crewai_headless_flow/hitl_policy.py`** — a single seam: `should_prompt(gate: HumanFeedbackGate, hf_config: dict, state: FlowState, context: GateContext) -> GateDecision`. `flow.py`'s five checkpoint call sites call this instead of today's inline `hf.get(gate, True)` lookup. This is the Strategy/Policy pattern (validated against LangGraph's "conditional edge" router-function precedent, which solves the identical problem).
- **`GateContext`** — a small typed dataclass, not a `dict[str, Any]`, so mypy can verify every trigger's required inputs are supplied at each call site. It does **not** carry a single `task_id`: see "Multi-task scan at `before_do_work`" below.
- **Trigger→gate mapping is hardcoded in `hitl_policy.py`, not configurable.** Resolved Question 9 (raised but left open last session): dropped `gate:` from each trigger's config schema. Every Phase 0 trigger has exactly one sensible gate (`repeated_task_failure` → `before_do_work`, `approaching_max_revisions` → `after_review`), and `should_prompt()` is already called once per gate with that gate as a parameter — a configurable field with only one legal value is speculative generality (YAGNI) and a footgun (e.g. pointing `repeated_task_failure` at `before_review` would be nonsensical). The YAML only carries `enabled` and each trigger's own thresholds (`min_attempts`, `within`).
- **`repeated_task_failure` counts *consecutive failures since the task's last success*, not a raw total of `task_executions` entries.** `_next_task_attempt` (`flow.py:1402-1412`) counts every execution regardless of outcome, and `_mark_tasks_for_revision` (`flow.py:3199-3210`) can reopen an already-`"done"` task to `"needs_revision"` if review sends it back — so a task that succeeded once and was legitimately reopened for revision rework must not look identical to a task stuck genuinely failing. `should_prompt()` walks a task's `task_executions` backward from the most recent entry, stopping at the first `success=True`, and compares that consecutive-failure count against `min_attempts`.
- **`min_attempts` off-by-one: it names the attempt number that should trigger a warning, not a failure count.** `min_attempts: 2` fires before the task's 2nd attempt is dispatched — i.e. after exactly 1 prior failure (`consecutive_failure_count >= min_attempts - 1`), not after 2 failures.
- **Multi-task scan at `before_do_work`, not a single `task_id`.** `_maybe_ask_human("do_work", ...)` (`flow.py:2806`) fires once per `do_work` stage invocation, *before* `execution_target_task_ids` is resolved (`flow.py:2803`) — so no single task is known yet at gate time. `should_prompt()` therefore scans every task not yet `"done"` (`"pending"`/`"needs_revision"`) at gate time, fires if **any** meets the `repeated_task_failure` threshold, and `GateContext` surfaces the worst-offending task's id + streak length for the checkpoint message rather than requiring a single task to be pre-selected by the caller. Tie-break when multiple tasks are simultaneously over threshold: pick the **longest failure streak**, then **lowest `task_id`** as a deterministic secondary key (both are already available from the scan with no extra state).
- **Failure streaks span revisions, not scoped to the current revision.** `TaskExecutionEntry.revision` (`state.py:106`) exists for audit/debugging purposes (which round produced this attempt), not to bound the streak. `repeated_task_failure` means "the coder keeps failing to complete this specific task," which is a property of the task across the whole run, not reset just because review moved the flow into a new revision — a task that failed in revision 1, got sent back by review, and fails again in revision 2 is exactly the pattern this trigger exists to catch.
- **`GateContext` and `GateDecision` shapes (Phase 0):**
  ```python
  @dataclass(frozen=True)
  class GateContext:
      tasks: tuple[TaskItem, ...] = ()   # snapshot of self.state.tasks at gate time; only before_do_work populates this

  @dataclass(frozen=True)
  class GateDecision:
      should_prompt: bool
      trigger_reason: TriggerReason | None = None   # None when a static gate (or no trigger) drove the decision
  ```
  `after_review`/`before_plan`/`before_review`/`before_finalize` call sites pass `GateContext()` (empty) since Phase 0 has no triggers needing extra state for them beyond what's already on `FlowState`.
- **Prompt mechanics are unaffected by *why* a gate fired.** `should_prompt()` only answers "prompt or not, and why" — everything downstream (`_human_feedback_prompt`'s message/options, `_enabled_human_feedback_actions`'s `action_allowlist`/`advanced_actions` gating at `flow.py:240-267`, `_parse_human_feedback_action`, `_record_human_feedback`) is untouched and already gate-keyed, not trigger-keyed. A conditional-trigger-fired prompt gets the exact same action menu a static gate would have gotten for that gate/stage; the only addition is the trigger's reason appended to the message.
- **Consequence of "legacy booleans are ignored entirely" under `mode: conditional`: gates with no Phase 0 trigger go permanently silent.** `before_plan`, `before_review`, and `before_finalize` have zero Phase 0 triggers targeting them, so under `mode: conditional` they never prompt, regardless of their legacy boolean value, until a future phase adds a trigger for them. This is called out explicitly (not just implied) in the README's new subsection and enforced by the doctor check below, since a user flipping `mode: conditional` without reading closely could be surprised that `before_finalize: true` stopped doing anything.
- **Doctor check (new, warn-only, mirrors the existing `auth.cursor_api_key` pattern at `diagnostics.py:568-579`):** when `human_feedback.mode == "conditional"`, warn if (a) every entry in `conditional.triggers.*` has `enabled: false` (mode is a no-op), or (b) any of `before_plan`/`before_review`/`before_finalize` is `true` while `mode: conditional` is set (that boolean is now dead config per the point above).
- **`TriggerReason`** — structured, not a formatted string: `kind: str` (mirrors trigger config keys 1:1) plus `detail`, typed as a **discriminated union of per-trigger dataclasses** (`RepeatedTaskFailureDetail(task_id, attempts)`, `ApproachingMaxRevisionsDetail(revisions, max_revisions)`) — zero `Any` anywhere. Modeled on LaunchDarkly's `reason.kind` + detail evaluation-reason pattern.
- **`HumanFeedbackEntry.trigger_reason: TriggerReason | None`** (`state.py:32-46`) — added now, not deferred, so the audit trail can distinguish "static gate fired" from "which trigger fired" from day one. Since it defaults to `None` and Pydantic fills missing keys with field defaults on `model_validate`, on-disk state snapshots written before this change (e.g. a paused/resumable run) load without modification — verify this explicitly with a test that `model_validate`s a `HumanFeedbackEntry` dict lacking the `trigger_reason` key at all, not just one that's `None`.
- **Config key**: `human_feedback.mode: "static" | "conditional"`, defaulting to `"static"` (zero behavior change for every existing config). Named `"conditional"`, not `"adaptive"` — Phase 0 is deterministic threshold logic, not judgment/inference, and "adaptive"/"judged" should stay reserved for any future LLM-in-the-loop trigger so the two are never conflated.
- **Semantics**: in `mode: "conditional"`, the five legacy gate booleans are **ignored entirely**, not reinterpreted as a "ceiling." Gate-firing is determined solely by `conditional.triggers.*` entries targeting that gate. (Rejected the "boolean becomes eligibility-only under conditional mode" alternative — that's the "one flag, two meanings depending on context" anti-pattern; kept orthogonal to how PagerDuty/Alertmanager separate routing eligibility from trigger conditions.)
- **CLI overrides**: extend `--override-human-feedback` to accept dotted nested keys (e.g. `--override-human-feedback conditional.triggers.repeated_task_failure.min_attempts=3`), writing into the same `config.human_feedback` tree it already owns. Do **not** route this through `--override-stage-extra` — that mechanism is hard-scoped to per-stage `config.workers[stage]` extras (`runtime_overrides.py:220-268` requires the first path segment to be a pipeline stage), a structurally different tree than the cross-stage `config.human_feedback` dict.
- **Existing example config packs** (34 files under `examples/configs/**/worker.yaml` that already set `human_feedback`) need **no edits** — `_validate_human_feedback`'s `{**DEFAULT_HUMAN_FEEDBACK, **(raw or {})}` merge (`config.py:203-214`) means new default keys apply automatically.

## Config shape

```yaml
human_feedback:
  enabled: false
  mode: "static"  # "static" (default, current behavior) | "conditional"
  before_plan: false
  before_do_work: true
  before_review: false
  after_review: false
  before_finalize: true
  capture_instructions: false
  advanced_actions: false
  action_allowlist: {}

  conditional:
    triggers:
      approaching_max_revisions:
        enabled: false
        within: 1
      repeated_task_failure:
        enabled: false
        min_attempts: 2
```

Note: `gate` is intentionally **not** a config key — the trigger→gate mapping is hardcoded in `hitl_policy.py` (see Design decisions above).

Every trigger defaults to `enabled: false`, so opting in requires two explicit changes (`mode: conditional` + a trigger's `enabled: true`) — "mostly autonomous by default" holds even for early adopters experimenting with the mode.

## Implementation outline

1. **`hitl_policy.py`** (new): `GateContext`, `TriggerReason`, the two detail dataclasses, `GateDecision`, and `should_prompt(...)`. Pure functions over `FlowState` — no I/O, no adapters.
2. **`config.py`**: extend `_validate_human_feedback` with `mode` (enum `static`/`conditional`) and a `conditional.triggers.*` nested schema (`enabled: bool`; `within`/`min_attempts` must be positive ints; no `gate` key — see Design decisions). Extend `_flatten`/override parsing in `runtime_overrides.py` for the dotted `--override-human-feedback` path.
3. **`state.py`**: add `TriggerReason`/detail dataclasses (or import from `hitl_policy.py`) and the new `HumanFeedbackEntry.trigger_reason` field; update `state.debug_report` serialization if it enumerates `HumanFeedbackEntry` fields explicitly.
4. **`flow.py`**: replace the five inline `hf.get(gate, True)` checks in `_maybe_ask_human` (`flow.py:611`) with `hitl_policy.should_prompt(...)`; build the right `GateContext` at each of the five call sites (only `before_do_work` needs task state for Phase 0, and it must be built from `self.state.tasks` *before* `execution_target_task_ids` is resolved, per the multi-task scan decision above); append the trigger's reason to the human-readable checkpoint message (e.g. `"Trigger: repeated_task_failure (task 3, 1 prior failure)"`) reusing the existing message-building call sites.
5. **`examples/configs/conditional-hitl/`** (new): one example pack demonstrating `mode: conditional` with both Phase 0 triggers enabled, mirroring the existing `examples/configs/*-gate/` pattern.
6. **Docs**: append **Gate** and **Trigger** glossary entries to the **existing** `CONTEXT.md` `## Language` section (created by the Domain Model Integration work, already holds its `Domain Model Integration` and `Target-repo domain model` entries — this is no longer a new file); `README.md` new "Conditional Human-in-the-Loop" subsection under "Enable Human-in-the-Loop" that explicitly calls out the silent-gate consequence above; `AGENTS.md` HITL bullet update plus adding `CONTEXT.md`, `docs/adr/`, and `docs/plans/` to the existing "Related Documentation" list (`AGENTS.md`'s doc index currently only lists `README.md`/`DESIGN.md`/`NOTICE`); record the `hitl_policy.py` seam in **both** a `DESIGN.md` addition (DESIGN.md is what AGENTS.md's Contribution Expectations name for architectural/extension-point changes) **and** a numbered ADR at `docs/adr/0003-hitl-policy-seam.md` for consistency with the existing `docs/adr/0001`/`0002` files. Caveats to honor when writing it: (a) the `docs/adr/` location convention originates in **ADR-0001**, whose status is "superseded" (ADR-0002 supersedes only its "target-repo-context portion," leaving the location convention itself ambiguous) — so confirm the location is still intended before relying on it; (b) `0003` is a **reservation** — verify no other ADR (e.g. a DMI fast-follow) has claimed it first and renumber if so.
7. **Tests** (offline only, no live CLI/LLM):
   - `hitl_policy.py` unit tests: construct `FlowState` fixtures with `revisions`/`max_revisions` and `task_executions` combinations; assert `should_prompt()` fires/doesn't fire correctly, static mode is unaffected, disabled triggers never fire, `mode: static` ignores `conditional.*` entirely. Cover the off-by-one boundary explicitly (`min_attempts: 2` fires after exactly 1 prior failure, not 2) and the consecutive-failure-since-last-success semantics (a task with success, then N failures, must count only the N trailing failures — not total executions). Cover the multi-task scan: multiple pending/needs_revision tasks at once, only one over threshold, `should_prompt()` still fires and surfaces the correct offending task.
   - `config.py`: schema validation tests for `mode`, invalid threshold types, and that a stray `gate` key under a trigger is rejected (not silently accepted) since it's no longer part of the schema.
   - `runtime_overrides.py`/CLI: dotted-path `--override-human-feedback` override tests.
   - `flow.py` integration: a full round-trip test that a `repeated_task_failure` trigger actually causes `_maybe_ask_human("do_work", ...)` to prompt when the legacy `before_do_work` boolean is `false` but conditional mode's trigger is enabled and its threshold is met, and that `HumanFeedbackEntry.trigger_reason` is persisted correctly.

## Out of scope for this plan

- Phase 1/2 triggers (parallel conflicts, sensitive paths, review issue volume, repair-retry flags, LLM-judged confidence).
- The openwiki (github.com/langchain-ai/openwiki) documentation-automation idea — no longer parked: it was discussed and shipped as the documentation-only **Domain Model Integration** feature (`docs/plans/2026-07-06-domain-model-integration.md`, ADR-0002). It is orthogonal **to Phase 0** (DMI is docs-only pass-through, zero Flow code; Phase 0 is Flow code, zero worker/context-file involvement) — their only overlap is the shared doc files handled in the `docs` todo above. This is *not* a claim of permanent orthogonality: a possible *future* functional join (a domain-aware trigger that reads domain content) is tracked in `docs/plans/2026-07-06-hitl-dmi-convergence.md` and is explicitly out of scope for Phase 0.
