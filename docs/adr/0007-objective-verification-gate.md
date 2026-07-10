# Review passes through an objective verification gate; delivery ships only verified work

## Status
Accepted.

"Review passed" used to mean "the worker said so plus an LLM agreed" — no test, lint, or build ever ran against the target repo (`CoderResult.tests_passed` was hardcoded `False` and no caller ran tests). `src/crewai_headless_flow/verification.py` adds a Flow-owned verification step at the top of every review round, and `delivery.py` gains real `push`/`pr` gated on it.

Decisions:

- **Verification is Flow-owned and never routed through a worker adapter.** Operator-declared commands (`verify.commands`) run as argv with no shell (`shlex.split` for strings; pipes/`&&` require a script), fail-fast, with a per-command `verify.timeout`. Timeouts map to exit 124 and launch failures to 127; `run_verification` never raises. The subprocess runner is injectable, so the suite stays 100% offline. The inspect/edit safety boundary is untouched.
- **`mode: gate` failures skip the LLM review entirely.** The command output tails become the issues of a synthesized `revise` decision, routed through the same `_record_review_decision_state` funnel an automated review uses — so the revise loop gets concrete evidence, and after-review HITL actions (`force-pass`, `target-tasks`) plus the `approaching_max_revisions` trigger keep working on verification failures (the escape hatch for a flaky suite). `mode: advisory` runs the commands and appends the rendered results to the review prompt as evidence instead.
- **Every review round re-verifies.** Each revise→do_work→review cycle re-enters the review loop, and a human `rerun-review` re-verifies too (they may have hand-fixed the tree while parked). Resume is free: both resume paths replay `review()`.
- **A human force-pass skips review-time verification but not the delivery predicate.** Human override is sovereign at the review stage, exactly as it already bypasses the LLM review — but push/PR still require the latest recorded verification to have passed.
- **The delivery predicate is mode-independent.** Whenever `verify.commands` is non-empty, `deliver.push`/`deliver.pr` require the most recent verification run to have passed — `advisory` only controls whether a failure short-circuits the review loop, never whether unverified work ships. Empty `commands` means the operator explicitly opted out of verification and owns the risk of `push: true` (doctor warns about this combination). Unverified requests report `blocked_unverified`.
- **A push or PR failure never demotes a successful local commit.** `status` stays `committed`; the failure rides the `push`/`pr` report fields and `state.errors`. PRs go through the `gh` CLI via an injectable runner (no `--base` — the base ref may be an arbitrary feature branch; `gh` targets the repo's default branch).
- **Known limitation:** the finalize worker writes the ADR/report file *after* the last verification run, so the delivered branch's docs delta is unverified. Re-verifying inside finalize is deliberate future work, not silent behavior.

State: `FlowState.verification_runs` records every round (revision-stamped); the debug report gains a `## Verification` section. Config: top-level `verify:` block (`commands`, `mode: gate|advisory`, `timeout`) and `deliver.remote`; per-run `--override-verify KEY=VALUE`. Cross-field rules: `deliver.pr` requires `deliver.push`, which requires `deliver.commit`.

See `docs/architecture/autonomy-gap-analysis.md` (Gaps 1 and 2, Phase 2) and ADR-0006.
