# Runs are checkpointed to a RunStore directory, not CrewAI's `@persist`

## Status
Accepted.

Before autonomy Phase 1, a run's state reached disk only if the operator passed `--state-file`, and only after the run returned — a process that died mid-run left nothing recoverable, and there was no run identity or history. (The old `state.py` docstring claimed `@persist` handled this; the decorator appeared nowhere.)

Every run started with `--runs-dir` (default `./runs`) now gets an identity and a durable home: `runs/<run_id>/` with `run_id = <timestamp>-<request-slug>-<uuid8>`, holding `state.json` and `debug_report.md`. `src/crewai_headless_flow/run_store.py` owns the directory; it is deliberately dependency-light (no flow/state imports, takes pre-serialized strings) so it stays trivially testable.

Two decisions worth recording:

- **Checkpointing piggybacks on `_refresh_debug_report()`**, not on explicit calls at each stage tail. That method is already invoked at every stage transition, review decision, task completion/failure, human-feedback record, and abort — a strict superset of the seams explicit wiring would cover, and it stays in sync by construction: any future state mutation that refreshes the report is automatically durable. Writes are atomic (unique temp file in the run dir + `os.replace`), so concurrent writers during parallel task batches can never produce a torn file — last complete write wins. A checkpoint write failure prints a warning and never kills the run.
- **CrewAI's `@persist` is deliberately rejected.** The bespoke `resume_headless_flow` path replays stage methods directly against a rehydrated `FlowState`; `@persist` would introduce a second, competing source of truth for "where was this run," and its SQLite store answers none of the questions the run dir does (human-readable state, report, approval files, future event logs).

Resume-from-crash builds on this: `resume_headless_flow` now also accepts `status == "running"` (what a crashed run's last checkpoint says) and synthesizes a checkpoint via `synthesize_crash_checkpoint`. The mapping re-runs the stage named by `last_stage` rather than advancing past it, because `last_stage` is **not** "last completed stage" — structured do_work and review both set it *before* their worker calls. `in_progress` tasks are reset to `pending`; done tasks stay done; review is read-only, so replay is safe.

See `docs/plans/2026-07-10-phase-1-unattended-reliability.md` and `docs/architecture/autonomy-gap-analysis.md` (Gap 3).
