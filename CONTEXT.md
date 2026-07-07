# crewai-headless-flow

A reusable, multi-agent CrewAI Flow that treats agent-skills as operating procedures and delegates code editing/running/testing to pluggable headless coding CLIs.

## Language

**Domain Model Integration**:
A documentation-only reliance on [OpenWiki](https://github.com/langchain-ai/openwiki)'s own `AGENTS.md`/`CLAUDE.md` self-registration in a target repository, which the `cursor` and `claude` workers already read natively — no Flow-side code, config, or maintenance. Superseded an earlier `domain-modeling`-skill-based design (Flow-authored `CONTEXT.md`/`docs/adr/`); see `docs/plans/2026-07-06-domain-model-integration.md`.
_Avoid_: Wiki, agent wiki, target-repo domain model (see below, now historical)

**Target-repo domain model** _(historical — see Domain Model Integration)_:
The `CONTEXT.md`/`CONTEXT-MAP.md` glossary and `docs/adr/` decision records that a target repository's own `domain-modeling`-skill-based conventions would hold. No longer written or read by this project's Flow as of the OpenWiki-based revision.
_Avoid_: Wiki, project wiki

**Gate** (human-in-the-loop):
A named checkpoint in the flow where operator input may be requested — one of `before_plan`, `before_do_work`, `before_review`, `after_review`, `before_finalize`. A gate maps to exactly one stage. Whether a gate actually prompts is decided by `hitl_policy.should_prompt()`: in `mode: static` by the gate's boolean, in `mode: conditional` by its [[Trigger]]s. See `src/crewai_headless_flow/hitl_policy.py`.
_Avoid_: hook, checkpoint (reserve "checkpoint" for the abort/resume snapshot)

**Trigger** (conditional human-in-the-loop):
A deterministic, state-derived condition that, under `mode: conditional`, decides whether a [[Gate]] fires. Phase 0 ships two: `repeated_task_failure` (at `before_do_work`) and `approaching_max_revisions` (at `after_review`). Trigger→gate mapping is hardcoded in `hitl_policy._TRIGGERS`; a trigger's config carries only `enabled` plus its own thresholds. When a trigger fires it produces a typed `TriggerReason` persisted on the audit entry.
_Avoid_: rule, condition (unqualified), signal
