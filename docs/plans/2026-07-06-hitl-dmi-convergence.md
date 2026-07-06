# Implementation Plan: Convergence of Conditional HITL and Domain Model Integration

## Overview

Today the two features are orthogonal: Domain Model Integration (DMI) is documentation-only pass-through (workers read OpenWiki's `AGENTS.md`/`CLAUDE.md` natively, Flow contributes zero code); Conditional HITL Phase 0 is all Flow code (`hitl_policy.py`, triggers, no worker involvement). They join in **two waves**: a shallow doc-coordination wave that's forced *now*, and a functional wave later where a domain-aware HITL trigger consumes domain content — which is the interesting one.

Related artifacts:
- `docs/plans/2026-07-06-domain-model-integration.md` (DMI final design, ADR-0002)
- `docs/plans/2026-07-06-conditional-hitl-phase-0.md` (Conditional HITL Phase 0, implementation-ready; relocated from Cursor's plan store into the repo)
- `docs/adr/0002-openwiki-replaces-domain-modeling-for-target-repo-context.md`
- `CONTEXT.md` (this repo's glossary)

## Architecture Decisions (the actual thesis)

- **They're two halves of one goal.** Both answer the user's stated aim ("mostly autonomous, unless it needs to interject"): DMI improves *grounding in* (fewer dumb interjections because the worker understands the target repo's domain); HITL improves *interrupts out* (only prompt when state warrants). The features fuse when a trigger becomes **context-aware** — i.e. HITL decides *when* to interrupt using *what the domain model knows*.

- **The bridge already has a name in both plans.** Phase 0 lists `sensitive_paths_touched` as a future (Phase 1/2) trigger. DMI's fast-follow #4 is "a Flow-side explicit read-and-inject step." These are the same seam viewed from two directions: to fire a domain-aware trigger, the Flow must *read* domain content — which is exactly the read-inject capability DMI deliberately declined to build in v1.

- **There's a genuine conflict to resolve at the join.** The DMI plan's "Resolved Design" states the design point **"the Flow never parses OpenWiki's structure"** (a design bullet in `docs/plans/2026-07-06-domain-model-integration.md`, *not* something ADR-0002 itself asserts — ADR-0002 only covers the choice of OpenWiki over the `domain-modeling` skill and the new dependency it introduces). Any domain-aware HITL trigger *requires* the Flow to read domain content — directly cutting against that "no Flow-side reading" stance. So the convergence isn't free; it forces a decision to either (a) revisit that design point via a new ADR for a narrow, well-scoped read path, or (b) keep triggers domain-blind and accept that HITL never benefits from the domain model. **This is the central open question, not a task.**

## Task List

### Phase 1: Doc-level convergence (forced now, independent of anything functional)

> **Status: PLAN-LEVEL RECONCILIATION DONE; execution deferred to Phase 0 (2026-07-06).** Tasks 1 & 2 were about *aligning the plans*, and that alignment is written into the two feature plans (see per-criterion boxes below). What is **not** done — and is intentionally deferred to Phase 0 implementation — is the actual `AGENTS.md` index edit, the `CONTEXT.md` append, and creating `docs/adr/0003`. Those are code/doc changes tracked as the Phase 0 `docs` todo, not part of this reconciliation. Phases 2–3 below remain open/speculative.
>
> **Note:** the Phase 0 plan has been relocated into the repo at `docs/plans/2026-07-06-conditional-hitl-phase-0.md` (it originated in Cursor's plan store, outside version control). The in-repo copy is now the authoritative reference; the original `.cursor/plans/` file is a stale duplicate.

**Task 1: Reconcile the shared doc files between the two features** — XS · plan alignment done, execution deferred
- **Description:** Both features write `CONTEXT.md`, the `AGENTS.md` "Related Documentation" index, and `DESIGN.md`. DMI already created `CONTEXT.md` + `docs/adr/`; Phase 0 still assumed it was creating them. Make Phase 0's `docs` todo an *append* and index both features' artifacts once.
- **Acceptance criteria:**
  - [x] Phase 0 plan's `docs` todo reworded create→append; the "CONTEXT.md (new — first terms)" language removed. *(done in the Phase 0 plan)*
  - [ ] `AGENTS.md` Related-Documentation index actually lists `CONTEXT.md`, `docs/adr/`, `docs/plans/`. *(planned in the Phase 0 `docs` todo; the file itself is edited during Phase 0 execution, not here)*
- **Verification:** the plan edits change no repo code, so no test run applies. `uv run pytest -m offline` (baseline: 562 passed) is the gate for the Phase 0 `docs` todo when it actually edits `AGENTS.md`.
- **Files actually touched by this reconciliation:** `conditional_hitl_phase_0_e45e0665.plan.md`, `docs/plans/2026-07-06-domain-model-integration.md`, this file. (`AGENTS.md` is *not* touched here — it is Phase 0 execution.)
- **Scope:** XS.

**Task 2: Decide the ADR home for the HITL seam** — XS · decided
- **Decision:** record the seam in **both** `DESIGN.md` (AGENTS.md's named home for architectural changes) **and** `docs/adr/0003-hitl-policy-seam.md` (consistency with existing `0001`/`0002`). Two caveats carried into the Phase 0 `docs` todo: the `docs/adr/` location convention originates in **ADR-0001** (status "superseded"; ADR-0002 supersedes only its target-repo-context portion, leaving the location convention ambiguous — confirm before relying on it), and `0003` is a **reservation** to re-verify against any ADR a DMI fast-follow might create first.
- **Acceptance criteria:**
  - [x] Decision recorded and referenced from the Phase 0 `docs` todo.
  - [ ] `docs/adr/0003-hitl-policy-seam.md` created. *(deferred to Phase 0 execution)*
- **Dependencies:** Task 1.
- **Scope:** XS.

### Checkpoint: Doc convergence
- [ ] No file-creation collisions remain between the two plans; both index cleanly in `AGENTS.md`.

### Phase 2: Build the bridge (only if the functional join is wanted)

**Task 3: Resolve the ADR-0002 tension — spike the read-inject seam** — S (design spike)
- **Description:** Prototype the narrowest possible Flow-side domain-content read (DMI fast-follow #4), scoped to feeding a trigger — *not* to injecting into prompts yet. Establish whether ADR-0002 gets a scoped exception or a new ADR supersedes it.
- **Acceptance criteria:**
  - [ ] A written decision (ADR) on whether/how the Flow may read domain content.
  - [ ] A `read_domain_context(target_repo) -> DomainContext | None` shape sketched (returns `None` when no OpenWiki/`CONTEXT.md` present — preserving zero-config default).
- **Verification:** [ ] Design reviewed; offline-testable with a fixture target repo, no live CLI.
- **Dependencies:** Task 2; **the open question above must be answered first.**
- **Scope:** S.

**Task 4: Implement the domain-read seam behind a typed interface** — M
- **Description:** Deep module, small interface (per the repo's `codebase-design` discipline): one function returning a typed `DomainContext`, no `Any`, offline-testable with fixtures. No trigger wiring yet.
- **Acceptance criteria:**
  - [ ] Reads target-repo `AGENTS.md`/`CLAUDE.md`/`CONTEXT.md` pointer without parsing OpenWiki internals (respects the ADR-0002 boundary as resolved in Task 3).
  - [ ] Returns typed, empty-safe result; zero behavior change when absent.
- **Verification:** [ ] New offline tests; full suite green.
- **Dependencies:** Task 3.
- **Scope:** M.

### Checkpoint: Bridge exists
- [ ] Flow can obtain typed domain context, gated and default-off, with no impact on existing runs.

### Phase 3: The actual join — a domain-aware trigger

**Task 5: Add `sensitive_paths_touched` as the first domain-aware trigger** — M
- **Description:** Extend `hitl_policy.should_prompt()` (Phase 0's seam) with a trigger that fires when a `do_work` diff touches paths the domain context flags sensitive. This is the concrete fusion: HITL's trigger machinery + DMI's domain knowledge.
- **Acceptance criteria:**
  - [ ] New trigger follows Phase 0's exact contract (`TriggerReason` discriminated-union detail, no `Any`, hardcoded trigger→gate mapping).
  - [ ] Fires only when `DomainContext` is present *and* a sensitive path is touched; silent otherwise.
  - [ ] Falls back to a hand-configured path list when no domain context exists (degrades gracefully).
- **Verification:** [ ] Offline trigger tests (present/absent domain context, sensitive/non-sensitive diff); suite green.
- **Dependencies:** Task 4 + **Phase 0 must be implemented first** (this extends its seam).
- **Scope:** M.

### Checkpoint: Features fused
- [ ] A single trigger demonstrably uses domain-model content to make an autonomy decision, end-to-end, offline.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| ADR-0002 "Flow never parses OpenWiki" blocks domain-aware triggers | **High** — kills the entire functional join | Resolve as Task 3's explicit decision *before* any code; scope the read to the pointer files workers already read, not OpenWiki internals |
| Phase 0 not yet implemented — Phase 3 has no seam to extend | High | Hard-sequence: Phase 0 ships before Phase 2/3 even starts |
| DMI's whole value prop is "zero Flow code"; adding a read seam erodes it | Medium | Keep the seam default-off and absent-safe; DMI's doc-only pass-through stays the default path, domain-read is opt-in |
| gemini/grok native-support gaps (DMI fast-follow 1–3) unresolved | Low for the join | Independent of the join; the read seam covers exactly the workers that lack native support anyway |

## Open Questions (need a decision before Phase 2)

- **Do we actually want the functional join, or just the doc cleanup?** Phase 1 is worth doing regardless. Phases 2–3 are speculative "probably going to join" territory and cost real code + an ADR reversal.
- **Is relaxing ADR-0002 acceptable** for a narrow, pointer-file-only read scoped to triggers? Everything downstream hinges on this.
- **Sequencing vs. Phase 0:** the functional join can't start until Conditional HITL Phase 0 is implemented (currently designed, zero code). Is Phase 0 still the next build, with this convergence as a follow-on?

## Recommendation

**Phase 1 is real and cheap — do it whenever Phase 0 lands.** Phases 2–3 are a legitimate future but they're gated on reversing an invariant just deliberately set in ADR-0002, so treat them as "maybe later" until Phase 0 is actually built and we've felt whether domain-blind triggers are good enough.
