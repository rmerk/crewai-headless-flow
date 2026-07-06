# crewai-headless-flow

A reusable, multi-agent CrewAI Flow that treats agent-skills as operating procedures and delegates code editing/running/testing to pluggable headless coding CLIs.

## Language

**Domain Model Integration**:
A documentation-only reliance on [OpenWiki](https://github.com/langchain-ai/openwiki)'s own `AGENTS.md`/`CLAUDE.md` self-registration in a target repository, which the `cursor` and `claude` workers already read natively — no Flow-side code, config, or maintenance. Superseded an earlier `domain-modeling`-skill-based design (Flow-authored `CONTEXT.md`/`docs/adr/`); see `docs/plans/2026-07-06-domain-model-integration.md`.
_Avoid_: Wiki, agent wiki, target-repo domain model (see below, now historical)

**Target-repo domain model** _(historical — see Domain Model Integration)_:
The `CONTEXT.md`/`CONTEXT-MAP.md` glossary and `docs/adr/` decision records that a target repository's own `domain-modeling`-skill-based conventions would hold. No longer written or read by this project's Flow as of the OpenWiki-based revision.
_Avoid_: Wiki, project wiki
