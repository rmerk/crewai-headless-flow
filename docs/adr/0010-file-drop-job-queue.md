# Triggering is a file-drop queue served by run subprocesses

## Status
Accepted.

Phase 3 (Gap 11) gives the platform its first standing trigger surface. Until now the only way to start work was a human typing `crewai-headless-flow run ...`; requests can now be dropped into a queue directory (`enqueue`) and drained by a long-running loop (`serve`), with `runs` providing the history listing over `runs/`.

Decisions:

- **File drop, not a server.** A job is a JSON file in `queue/pending/`; anything that can write a file — cron, a webhook receiver, a ticket integration, a human — is a trigger. The platform keeps zero network code (the same posture as the escalation channel's `command` seam): whatever listens to the outside world lives outside the platform and converges on `enqueue`.
- **Jobs run as `run` subprocesses — no second execution path.** `serve` shells into the existing CLI (`python -m crewai_headless_flow run ...`) with one run dir, one delivery branch, and one `--state-file` result per job. Every `run` behavior (preflight, verify gate, delivery, escalation) applies to queued jobs by construction, and a crashing run cannot take the serve loop down. stdin is `/dev/null`: serve jobs are headless by construction, so HITL gates must be off/conditional; parked escalations land in `failed/` with their `run_status` recorded and are resumed manually via `run --resume-state-file`.
- **Claims are atomic renames.** `pending/ → running/` via `os.rename`; job files are written atomically (temp + `os.replace`). Lexical order of the timestamp-prefixed job ids gives FIFO. Terminal states are `done/` (exit 0) and `failed/` (everything else), with per-job logs in `logs/` and the final FlowState in `results/`.
- **One serve loop per queue directory.** On startup, orphaned `running/` jobs from a crashed serve are requeued. Two loops on one queue would requeue each other's live jobs; the concurrency knob is `--max-concurrent` within a single loop, not multiple loops.
- **Offline-testable at the same boundaries as everything else.** The subprocess launcher, argv builder, clock, and sleep are injectable; `--once` drains the queue and exits, which is both the test mode and the cron mode.

Deliberately deferred: the first-party ticket trigger (absorbing the external `invoke-ticket-flow` integration) — its interface is exactly an `enqueue` call, but which tracker, credentials, and mapping to use are operator decisions that do not belong in this repository's core.

Config surface: none in `worker.yaml` — queueing is a CLI/ops concern (`--queue-dir`, `--max-concurrent`, `--poll-interval`, `--once`). Job files carry per-run overrides (`overrides: {verify: [...], ...}`) that map 1:1 onto the documented `--override-*` flags.

See `docs/architecture/autonomy-gap-analysis.md` (Gap 11, Phase 3) and `src/crewai_headless_flow/job_queue.py`.
