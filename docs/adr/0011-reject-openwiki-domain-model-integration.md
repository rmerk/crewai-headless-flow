# Domain Model Integration / OpenWiki pass-through is rejected

## Status
Accepted. Supersedes ADR-0001 and ADR-0002 for this initiative.

We designed, then documented, a "Domain Model Integration" path that relied on [OpenWiki](https://github.com/langchain-ai/openwiki) self-registering into a target repo's `AGENTS.md`/`CLAUDE.md` so workers would pick up domain context with zero Flow code. That initiative is abandoned: no further OpenWiki pass-through docs, no HITL↔DMI convergence, and no Flow-side read of OpenWiki (or a Flow-authored `CONTEXT.md`/`docs/adr/` substitute) for target-repo grounding.

Workers may still natively read whatever `AGENTS.md`/`CLAUDE.md` a target repo already has — that is ordinary CLI behavior, not a feature of this project. Target-repo domain grounding is out of scope for `crewai-headless-flow` until a future ADR reopens it with a different mechanism.
