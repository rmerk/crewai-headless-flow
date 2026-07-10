# Git delivery is Flow-owned, commit-only, and on a fresh branch

## Status
Accepted.

A "completed" run used to end as a dirty working tree on whatever branch the operator had checked out — "PR-ready change" did not exist in the platform. `src/crewai_headless_flow/delivery.py` adds an opt-in delivery step at the end of `finalize`: commit the flow's own changed files onto a fresh `{branch_prefix}{run_id}` branch.

Decisions:

- **The Flow owns delivery; adapters keep zero git responsibility.** `delivery.py` is the only git *writer* in the platform (existing git usage elsewhere is read-only probes and `ls-files`). The git runner is injectable; branch/commit tests use real `git` in `tmp_path` repos (zero network), failure paths use fakes.
- **Commit-only in Phase 1.** `push`/`pr` are validated config keys but ignored with a log line (`requested_not_implemented` in the report): shipping work off the machine waits for Phase 2's verification gate, so delivery never publishes unverified work.
- **Hard guardrails.** Always a fresh branch (collision → `-2`, `-3`, …); refuse if the computed name is in `protected_branches`; stage only the flow's changed files with per-path `git add --` — never `git add -A`, because preflight tolerates pre-existing operator dirt with a warning and delivery must not launder it into the flow's commit; unsafe (absolute/`..`) paths are skipped and reported, not staged; no `--force`, no `reset`, no branch deletion anywhere. Detached and unborn HEAD are supported (`checkout -b` from unborn yields a root commit).
- **`deliver()` never raises, and a delivery failure does not fail the run.** The work exists in the tree; delivery is packaging. A failure records `state.delivery_report.status == "failed"` plus an entry in `state.errors` while the run stays `completed`.
- **finalize snapshot-diffs around its worker call.** Adapters under-report `changed_files` (grok always returns `[]`), so without the diff the ADR/report file finalize just wrote would routinely miss the commit.
- **Documented consequence:** a delivered run ends with the target repo checked out on the `flow/<run_id>` branch — deliberately, so the operator inspects exactly what the flow produced.

Config: top-level `deliver:` block in `worker.yaml`; per-run `--override-deliver KEY=VALUE` (e.g. `enabled=true`).

See `docs/plans/2026-07-10-phase-1-unattended-reliability.md` and `docs/architecture/autonomy-gap-analysis.md` (Gap 2).
