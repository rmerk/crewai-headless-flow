# Runs leave a JSONL event log; diagnostics use logging, product output stays print

## Status
Accepted.

Diagnosing a run used to mean reading the markdown debug report; nothing recorded *when* things happened, and all narration was bare `print()`. Two changes:

## JSONL event log

`RunStore.append_event` appends one pre-serialized JSON line to `runs/<run_id>/events.jsonl` — append-only (a resumed run continues the same file), unlike the replace-based atomic snapshots. `flow._log_event` builds the envelope and inherits the checkpoint durability policy: no run dir → no-op; a write failure logs a warning and never kills the run.

- **Envelope:** `{ts, run_id, revision, kind, ...kind-specific fields}`. Timestamps come from an injectable clock seam (`_now_fn`), so tests are deterministic — the same pattern as `generate_run_id(now=...)`.
- **No new taxonomy.** The single `_record_history` funnel emits every `FlowHistoryEntry` kind as an event (`task_complete`, `task_failed`, `review_decision`, the replan/targeting kinds), plus lifecycle kinds at existing seams: `stage_start`, `human_feedback`, `human_abort`, `verification`, `delivery`, `run_completed`, `run_failed`. Kind-specific fields are additive-only.

## Logging policy

Diagnostic narration moved from `print()` to the `logging` module (`flow.py`, `delivery.py`, `escalation.py`; per-module `logging.getLogger(__name__)`), keeping the `[Flow]`/`[Delivery]`/`[Escalation]` message prefixes verbatim so terminal output is unchanged. Failure-ish messages log at WARNING, narration at INFO.

Deliberately kept as `print`:

- the interactive `[Human Feedback]` conversation around `input()` — a gate prompt must reach the terminal regardless of logging configuration;
- `cli.py`'s run-state/doctor/preflight rendering — that output *is* the CLI's contract;
- `config.print_mapping()` (the startup table) and the legacy `flow_spike.py`.

`cli.main` configures only the `crewai_headless_flow` package logger (stdout handler, plain `%(message)s`, INFO, `propagate=False`) and only when no handlers exist — deliberately **not** `logging.basicConfig`, because crewai/litellm configure the root logger themselves and would fight it, and library users who configure the package logger stay in control. A per-run `flow.log` FileHandler is possible future work; `events.jsonl` is the durable trail.

See `docs/architecture/autonomy-gap-analysis.md` (Gap 9, Phase 2) and ADR-0004.
