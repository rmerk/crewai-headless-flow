"""
File-drop job queue + serve loop (autonomy Phase 3, Gap 11).

The platform's first standing trigger surface: requests are dropped into a
queue directory as JSON job files and a single ``serve`` loop drains them by
shelling into the existing ``run`` CLI path — one subprocess, one run dir,
one delivery branch per job. A queue directory looks like::

    queue/
      pending/   <job_id>.json   enqueued, waiting (lexical order == FIFO)
      running/   <job_id>.json   claimed by the serve loop
      done/      <job_id>.json   run exited 0 (status "completed")
      failed/    <job_id>.json   run exited non-zero, or could not launch
      logs/      <job_id>.log    combined stdout/stderr of the run
      results/   <job_id>.state.json  final FlowState (via run --state-file)

Design notes:

- **File drop, not a server.** Anything that can write a file (cron, a
  webhook receiver, a ticket integration, a human) can enqueue; the
  platform itself keeps zero network code, mirroring the escalation
  channel's ``command`` seam.
- **Claims are atomic.** ``os.rename`` from ``pending/`` to ``running/``
  either wins or raises — two pollers can never both claim a job. Job
  files are also written atomically (temp + ``os.replace``) so a claim
  never reads a torn file.
- **Runs are subprocesses.** A crashing run cannot take the serve loop
  with it, concurrency is real OS concurrency, and the job inherits every
  ``run`` behavior (preflight, run dir, verify gate, delivery) without a
  second code path. stdin is ``/dev/null``: serve jobs are headless by
  construction, so HITL gates must be off or conditional (escalation
  parks runs via the file/command channel instead).
- **Single serve loop per queue dir.** On startup, orphaned ``running/``
  jobs (a previous serve crashed mid-run) are requeued to ``pending/``.
  Running two serve loops against one queue would re-claim each other's
  crashed leftovers; don't.
- **Offline-testable.** The subprocess boundary (``launcher``), clock and
  sleep are injectable; tests drive the loop with fake processes and
  ``once=True``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from pydantic import BaseModel, Field

from .run_store import generate_run_id

logger = logging.getLogger(__name__)

PENDING_DIR = "pending"
RUNNING_DIR = "running"
DONE_DIR = "done"
FAILED_DIR = "failed"
LOGS_DIR = "logs"
RESULTS_DIR = "results"

_JOB_STATE_DIRS = (PENDING_DIR, RUNNING_DIR, DONE_DIR, FAILED_DIR)

# Keys match the CLI flags: an entry ("verify", ["mode=advisory"]) becomes
# ``--override-verify mode=advisory`` on the spawned run's argv.
SUPPORTED_OVERRIDE_KINDS = frozenset(
    {
        "skill",
        "default-worker",
        "default-model",
        "default-timeout",
        "worker",
        "model",
        "timeout",
        "stage-extra",
        "human-feedback",
        "human-feedback-action",
        "deliver",
        "verify",
        "worker-binary",
    }
)


class QueueJob(BaseModel):
    """One queued request; result fields are filled in by the serve loop."""

    job_id: str
    request: str
    target_repo: str
    created_at: str = ""
    max_revisions: int | None = None
    config_dir: str | None = None
    overrides: dict[str, list[str]] = Field(default_factory=dict)

    exit_code: int | None = None
    run_id: str | None = None
    run_status: str | None = None
    pr_url: str | None = None
    branch: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


class ServeReport(BaseModel):
    """Outcome of one serve invocation (meaningful for ``once=True``)."""

    processed: int = 0
    done: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    requeued: list[str] = Field(default_factory=list)


class JobProcess(Protocol):
    """The slice of subprocess.Popen the serve loop consumes."""

    returncode: int | None

    def poll(self) -> int | None: ...


JobLauncher = Callable[[list[str], Path], JobProcess]
ArgvBuilder = Callable[..., list[str]]


def ensure_queue_dirs(queue_dir: Path | str) -> Path:
    queue_dir = Path(queue_dir)
    for name in (*_JOB_STATE_DIRS, LOGS_DIR, RESULTS_DIR):
        (queue_dir / name).mkdir(parents=True, exist_ok=True)
    return queue_dir


def new_job(
    request: str,
    target_repo: str,
    *,
    max_revisions: int | None = None,
    config_dir: str | None = None,
    overrides: dict[str, list[str]] | None = None,
    now: datetime | None = None,
    uuid_hex: str | None = None,
) -> QueueJob:
    """Build a QueueJob with a sortable id (same shape as a run id)."""
    unknown = sorted(set(overrides or {}) - SUPPORTED_OVERRIDE_KINDS)
    if unknown:
        supported = ", ".join(sorted(SUPPORTED_OVERRIDE_KINDS))
        raise ValueError(
            f"Unsupported override kinds: {', '.join(unknown)}. Supported: {supported}"
        )
    stamp = now or datetime.now()
    return QueueJob(
        job_id=generate_run_id(request, now=stamp, uuid_hex=uuid_hex),
        request=request,
        target_repo=str(target_repo),
        created_at=stamp.isoformat(timespec="seconds"),
        max_revisions=max_revisions,
        config_dir=config_dir,
        overrides=dict(overrides or {}),
    )


def enqueue_job(queue_dir: Path | str, job: QueueJob) -> Path:
    """Atomically drop a job into ``pending/``; returns the job file path."""
    queue_dir = ensure_queue_dirs(queue_dir)
    target = queue_dir / PENDING_DIR / f"{job.job_id}.json"
    if target.exists():
        raise FileExistsError(f"Job already queued: {target}")
    _atomic_write_json(target, job)
    logger.info(f"[Queue] Enqueued job {job.job_id} -> {target}")
    return target


def claim_next_job(queue_dir: Path | str) -> QueueJob | None:
    """Claim the oldest pending job by atomic rename; None when empty.

    Job ids start with a timestamp, so lexical order is FIFO. A rename
    that loses a race (file already claimed) just moves on to the next
    candidate.
    """
    queue_dir = Path(queue_dir)
    pending = queue_dir / PENDING_DIR
    if not pending.is_dir():
        return None
    for candidate in sorted(pending.glob("*.json")):
        claimed = queue_dir / RUNNING_DIR / candidate.name
        try:
            os.rename(candidate, claimed)
        except OSError:
            continue  # lost the claim race (or a transient fs error); next
        try:
            return QueueJob.model_validate_json(claimed.read_text())
        except Exception as exc:
            # Poisoned job file: park it in failed/ rather than crash-loop.
            logger.warning(f"[Queue] Unreadable job file {candidate.name}: {exc}")
            os.replace(claimed, queue_dir / FAILED_DIR / candidate.name)
            continue
    return None


def finish_job(
    queue_dir: Path | str,
    job: QueueJob,
    *,
    succeeded: bool,
) -> Path:
    """Move a running job to ``done/`` or ``failed/`` with its result fields."""
    queue_dir = Path(queue_dir)
    running = queue_dir / RUNNING_DIR / f"{job.job_id}.json"
    dest_dir = DONE_DIR if succeeded else FAILED_DIR
    target = queue_dir / dest_dir / f"{job.job_id}.json"
    _atomic_write_json(target, job)
    running.unlink(missing_ok=True)
    return target


def requeue_orphaned_running_jobs(queue_dir: Path | str) -> list[str]:
    """Move ``running/`` leftovers from a crashed serve back to ``pending/``."""
    queue_dir = Path(queue_dir)
    requeued: list[str] = []
    running = queue_dir / RUNNING_DIR
    if not running.is_dir():
        return requeued
    for orphan in sorted(running.glob("*.json")):
        os.replace(orphan, queue_dir / PENDING_DIR / orphan.name)
        requeued.append(orphan.stem)
        logger.warning(
            f"[Queue] Requeued orphaned running job {orphan.stem} "
            "(previous serve did not finish it)."
        )
    return requeued


def list_jobs(queue_dir: Path | str) -> dict[str, list[QueueJob]]:
    """Snapshot every job by state (pending/running/done/failed)."""
    queue_dir = Path(queue_dir)
    snapshot: dict[str, list[QueueJob]] = {}
    for state in _JOB_STATE_DIRS:
        jobs: list[QueueJob] = []
        state_dir = queue_dir / state
        if state_dir.is_dir():
            for path in sorted(state_dir.glob("*.json")):
                try:
                    jobs.append(QueueJob.model_validate_json(path.read_text()))
                except Exception:
                    continue
        snapshot[state] = jobs
    return snapshot


def build_run_argv(
    job: QueueJob,
    *,
    runs_dir: Path | str | None,
    state_file: Path | str,
) -> list[str]:
    """The exact ``run`` invocation a job maps to — no second run code path."""
    argv = [
        sys.executable,
        "-m",
        "crewai_headless_flow",
        "run",
        "--request",
        job.request,
        "--target-repo",
        job.target_repo,
        "--state-file",
        str(state_file),
        "--format",
        "json",
    ]
    if job.max_revisions is not None:
        argv += ["--max-revisions", str(job.max_revisions)]
    if job.config_dir:
        argv += ["--config-dir", job.config_dir]
    if runs_dir is not None:
        argv += ["--runs-dir", str(runs_dir)]
    for kind in sorted(job.overrides):
        for value in job.overrides[kind]:
            argv += [f"--override-{kind}", value]
    return argv


def launch_run_subprocess(argv: list[str], log_path: Path) -> "subprocess.Popen[bytes]":
    """Default launcher: run detached from the TTY with a per-job log file.

    stdin is /dev/null so a stray HITL ``input()`` fails fast instead of
    hanging the queue.
    """
    with log_path.open("ab") as log:
        return subprocess.Popen(
            argv,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )


def serve_queue(
    queue_dir: Path | str,
    *,
    runs_dir: Path | str | None = None,
    max_concurrent: int = 1,
    poll_interval: float = 2.0,
    once: bool = False,
    launcher: JobLauncher | None = None,
    argv_builder: ArgvBuilder = build_run_argv,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], datetime] = datetime.now,
) -> ServeReport:
    """Drain the queue: claim -> spawn ``run`` -> record result, forever.

    ``once=True`` processes until pending and running are both empty, then
    returns (the offline-testable mode and the cron-friendly mode).
    KeyboardInterrupt stops claiming, waits for in-flight jobs, and returns.
    """
    if max_concurrent < 1:
        raise ValueError("max_concurrent must be at least 1")
    if launcher is None:
        # Resolved at call time (not a def-time default) so tests can
        # monkeypatch the module attribute through the CLI path too.
        launcher = launch_run_subprocess
    queue_dir = ensure_queue_dirs(queue_dir)
    report = ServeReport(requeued=requeue_orphaned_running_jobs(queue_dir))

    running: dict[str, tuple[QueueJob, JobProcess]] = {}
    draining = False
    while True:
        try:
            _reap_finished_jobs(queue_dir, running, report, now_fn=now_fn)

            while not draining and len(running) < max_concurrent:
                job = claim_next_job(queue_dir)
                if job is None:
                    break
                _start_job(
                    queue_dir,
                    job,
                    running,
                    report,
                    runs_dir=runs_dir,
                    launcher=launcher,
                    argv_builder=argv_builder,
                    now_fn=now_fn,
                )

            queue_empty = not any((queue_dir / PENDING_DIR).glob("*.json"))
            if not running and (draining or (once and queue_empty)):
                return report
            sleep_fn(poll_interval)
        except KeyboardInterrupt:
            if draining or not running:
                logger.warning("[Serve] Interrupted; exiting immediately.")
                return report
            draining = True
            logger.warning(
                f"[Serve] Interrupted; waiting for {len(running)} running "
                "job(s), no new claims. Interrupt again to exit now."
            )


def _start_job(
    queue_dir: Path,
    job: QueueJob,
    running: dict[str, tuple[QueueJob, JobProcess]],
    report: ServeReport,
    *,
    runs_dir: Path | str | None,
    launcher: JobLauncher,
    argv_builder: ArgvBuilder,
    now_fn: Callable[[], datetime],
) -> None:
    job.started_at = now_fn().isoformat(timespec="seconds")
    state_file = queue_dir / RESULTS_DIR / f"{job.job_id}.state.json"
    log_path = queue_dir / LOGS_DIR / f"{job.job_id}.log"
    argv = argv_builder(job, runs_dir=runs_dir, state_file=state_file)
    try:
        proc = launcher(argv, log_path)
    except OSError as exc:
        job.finished_at = now_fn().isoformat(timespec="seconds")
        job.error = f"Failed to launch run: {exc}"
        finish_job(queue_dir, job, succeeded=False)
        report.processed += 1
        report.failed.append(job.job_id)
        logger.warning(f"[Serve] Job {job.job_id} failed to launch: {exc}")
        return
    running[job.job_id] = (job, proc)
    logger.info(f"[Serve] Job {job.job_id} started (log: {log_path}).")


def _reap_finished_jobs(
    queue_dir: Path,
    running: dict[str, tuple[QueueJob, JobProcess]],
    report: ServeReport,
    *,
    now_fn: Callable[[], datetime],
) -> None:
    for job_id in list(running):
        job, proc = running[job_id]
        exit_code = proc.poll()
        if exit_code is None:
            continue
        del running[job_id]
        job.exit_code = exit_code
        job.finished_at = now_fn().isoformat(timespec="seconds")
        _attach_run_result(queue_dir, job)
        succeeded = exit_code == 0
        finish_job(queue_dir, job, succeeded=succeeded)
        report.processed += 1
        (report.done if succeeded else report.failed).append(job_id)
        outcome = "done" if succeeded else f"failed (exit {exit_code})"
        log = logger.info if succeeded else logger.warning
        log(
            f"[Serve] Job {job_id} {outcome}"
            + (f" | run {job.run_id} [{job.run_status}]" if job.run_id else "")
        )


def _attach_run_result(queue_dir: Path, job: QueueJob) -> None:
    """Read the run's final state file (if any) onto the job record."""
    state_file = queue_dir / RESULTS_DIR / f"{job.job_id}.state.json"
    try:
        data: Any = json.loads(state_file.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    run_id = data.get("run_id")
    status = data.get("status")
    job.run_id = str(run_id) if run_id else None
    job.run_status = str(status) if status else None
    delivery = data.get("delivery_report")
    if isinstance(delivery, dict):
        pr_url = delivery.get("pr_url")
        branch = delivery.get("branch")
        job.pr_url = str(pr_url) if pr_url else None
        job.branch = str(branch) if branch else None


def _atomic_write_json(target: Path, job: QueueJob) -> None:
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(job.model_dump_json(indent=2))
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
