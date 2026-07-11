"""
Gap 11 (autonomy Phase 3): file-drop job queue + serve loop.

Queue-store tests exercise the real filesystem in tmp_path; serve-loop
tests inject fake launchers/processes; one test drives the real
subprocess launcher with a tiny ``python -c`` job (offline, no CLIs).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from crewai_headless_flow.job_queue import (
    DONE_DIR,
    FAILED_DIR,
    PENDING_DIR,
    RESULTS_DIR,
    RUNNING_DIR,
    QueueJob,
    build_run_argv,
    claim_next_job,
    enqueue_job,
    finish_job,
    launch_run_subprocess,
    list_jobs,
    new_job,
    requeue_orphaned_running_jobs,
    serve_queue,
)


pytestmark = pytest.mark.offline

NOW = datetime(2026, 7, 11, 12, 0, 0)


def _job(
    request: str = "add auth", *, uuid_hex: str = "abcd1234", **kwargs
) -> QueueJob:
    return new_job(request, "/tmp/target", now=NOW, uuid_hex=uuid_hex, **kwargs)


# --- job construction ---------------------------------------------------------


def test_new_job_builds_sortable_id_and_records_fields():
    job = _job(max_revisions=3, config_dir="/tmp/config")

    assert job.job_id == "20260711-120000-add-auth-abcd1234"
    assert job.request == "add auth"
    assert job.target_repo == "/tmp/target"
    assert job.created_at == "2026-07-11T12:00:00"
    assert job.max_revisions == 3
    assert job.config_dir == "/tmp/config"
    assert job.exit_code is None


def test_new_job_rejects_unknown_override_kinds():
    with pytest.raises(ValueError, match="Unsupported override kinds: bogus"):
        _job(overrides={"bogus": ["x=y"]})


def test_new_job_accepts_supported_override_kinds():
    job = _job(overrides={"verify": ["mode=advisory"], "worker": ["do_work=claude"]})

    assert job.overrides == {
        "verify": ["mode=advisory"],
        "worker": ["do_work=claude"],
    }


# --- enqueue / claim / finish ---------------------------------------------------


def test_enqueue_writes_pending_job_file(tmp_path: Path):
    queue = tmp_path / "queue"
    job = _job()

    path = enqueue_job(queue, job)

    assert path == queue / PENDING_DIR / f"{job.job_id}.json"
    assert QueueJob.model_validate_json(path.read_text()) == job


def test_enqueue_refuses_duplicate_job_id(tmp_path: Path):
    queue = tmp_path / "queue"
    job = _job()
    enqueue_job(queue, job)

    with pytest.raises(FileExistsError):
        enqueue_job(queue, job)


def test_claim_is_fifo_and_moves_to_running(tmp_path: Path):
    queue = tmp_path / "queue"
    older = new_job("first", "/tmp/t", now=datetime(2026, 7, 11, 11), uuid_hex="aa")
    newer = new_job("second", "/tmp/t", now=datetime(2026, 7, 11, 12), uuid_hex="bb")
    enqueue_job(queue, newer)
    enqueue_job(queue, older)

    claimed = claim_next_job(queue)

    assert claimed is not None
    assert claimed.request == "first"
    assert (queue / RUNNING_DIR / f"{older.job_id}.json").exists()
    assert not (queue / PENDING_DIR / f"{older.job_id}.json").exists()
    assert (queue / PENDING_DIR / f"{newer.job_id}.json").exists()


def test_claim_returns_none_when_empty(tmp_path: Path):
    assert claim_next_job(tmp_path / "queue") is None


def test_claim_parks_poisoned_job_file_in_failed(tmp_path: Path):
    queue = tmp_path / "queue"
    good = _job()
    enqueue_job(queue, good)
    poisoned = queue / PENDING_DIR / "00000000-000000-poison-x.json"
    poisoned.write_text("{not json")

    claimed = claim_next_job(queue)

    assert claimed is not None and claimed.job_id == good.job_id
    assert (queue / FAILED_DIR / poisoned.name).exists()


def test_finish_job_moves_to_done_with_result_fields(tmp_path: Path):
    queue = tmp_path / "queue"
    job = _job()
    enqueue_job(queue, job)
    claimed = claim_next_job(queue)
    assert claimed is not None
    claimed.exit_code = 0
    claimed.run_id = "run-1"

    target = finish_job(queue, claimed, succeeded=True)

    assert target == queue / DONE_DIR / f"{job.job_id}.json"
    assert not (queue / RUNNING_DIR / f"{job.job_id}.json").exists()
    stored = QueueJob.model_validate_json(target.read_text())
    assert stored.exit_code == 0
    assert stored.run_id == "run-1"


def test_requeue_orphaned_running_jobs(tmp_path: Path):
    queue = tmp_path / "queue"
    job = _job()
    enqueue_job(queue, job)
    claim_next_job(queue)

    requeued = requeue_orphaned_running_jobs(queue)

    assert requeued == [job.job_id]
    assert (queue / PENDING_DIR / f"{job.job_id}.json").exists()


def test_list_jobs_snapshots_every_state(tmp_path: Path):
    queue = tmp_path / "queue"
    pending = new_job("pending", "/tmp/t", now=NOW, uuid_hex="aa")
    finished = new_job("finished", "/tmp/t", now=NOW, uuid_hex="bb")
    enqueue_job(queue, pending)
    enqueue_job(queue, finished)
    claimed = claim_next_job(queue)
    assert claimed is not None
    finish_job(queue, claimed, succeeded=True)

    snapshot = list_jobs(queue)

    assert [job.job_id for job in snapshot["pending"]] == [pending.job_id]
    assert [job.job_id for job in snapshot["done"]] == [claimed.job_id]
    assert snapshot["running"] == []
    assert snapshot["failed"] == []


# --- run argv mapping -----------------------------------------------------------


def test_build_run_argv_maps_job_onto_the_run_cli():
    job = _job(
        max_revisions=3,
        config_dir="/tmp/config",
        overrides={"verify": ["mode=advisory"], "deliver": ["enabled=true"]},
    )

    argv = build_run_argv(job, runs_dir="/tmp/runs", state_file="/tmp/q/r.json")

    assert argv == [
        sys.executable,
        "-m",
        "crewai_headless_flow",
        "run",
        "--request",
        "add auth",
        "--target-repo",
        "/tmp/target",
        "--state-file",
        "/tmp/q/r.json",
        "--format",
        "json",
        "--max-revisions",
        "3",
        "--config-dir",
        "/tmp/config",
        "--runs-dir",
        "/tmp/runs",
        "--override-deliver",
        "enabled=true",
        "--override-verify",
        "mode=advisory",
    ]


# --- serve loop with fake launchers ---------------------------------------------


class FakeProc:
    """Popen double that stays alive for N polls, then exits."""

    def __init__(self, exit_code: int = 0, polls_until_exit: int = 1):
        self.exit_code = exit_code
        self.polls_until_exit = polls_until_exit
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.polls_until_exit > 0:
            self.polls_until_exit -= 1
            return None
        self.returncode = self.exit_code
        return self.exit_code


class FakeLauncher:
    """Records launches and tracks how many jobs were live at once."""

    def __init__(self, exit_codes: dict[str, int] | None = None):
        self.exit_codes = exit_codes or {}
        self.launched: list[list[str]] = []
        self.live: list[FakeProc] = []
        self.high_water = 0

    def __call__(self, argv: list[str], log_path: Path) -> FakeProc:
        self.launched.append(argv)
        request = argv[argv.index("--request") + 1]
        proc = FakeProc(exit_code=self.exit_codes.get(request, 0))
        original_poll = proc.poll

        def tracked_poll() -> int | None:
            code = original_poll()
            if code is not None and proc in self.live:
                self.live.remove(proc)
            return code

        proc.poll = tracked_poll  # type: ignore[method-assign]
        self.live.append(proc)
        self.high_water = max(self.high_water, len(self.live))
        return proc


def test_serve_once_drains_queue_and_moves_jobs_to_done(tmp_path: Path):
    queue = tmp_path / "queue"
    first = new_job("first", "/tmp/t", now=datetime(2026, 7, 11, 11), uuid_hex="aa")
    second = new_job("second", "/tmp/t", now=datetime(2026, 7, 11, 12), uuid_hex="bb")
    enqueue_job(queue, first)
    enqueue_job(queue, second)
    launcher = FakeLauncher()

    report = serve_queue(
        queue,
        once=True,
        launcher=launcher,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert report.processed == 2
    assert sorted(report.done) == sorted([first.job_id, second.job_id])
    assert report.failed == []
    assert launcher.high_water == 1  # max_concurrent defaults to 1
    assert {p.name for p in (queue / DONE_DIR).glob("*.json")} == {
        f"{first.job_id}.json",
        f"{second.job_id}.json",
    }
    stored = QueueJob.model_validate_json(
        (queue / DONE_DIR / f"{first.job_id}.json").read_text()
    )
    assert stored.exit_code == 0
    assert stored.started_at and stored.finished_at


def test_serve_respects_max_concurrent(tmp_path: Path):
    queue = tmp_path / "queue"
    for index in range(3):
        enqueue_job(
            queue,
            new_job(f"job {index}", "/tmp/t", now=NOW, uuid_hex=f"{index:08d}"),
        )
    launcher = FakeLauncher()

    serve_queue(
        queue,
        once=True,
        max_concurrent=2,
        launcher=launcher,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert len(launcher.launched) == 3
    assert launcher.high_water == 2


def test_serve_records_run_result_and_routes_failures(tmp_path: Path):
    queue = tmp_path / "queue"
    job = _job("failing job")
    enqueue_job(queue, job)

    class ResultWritingLauncher(FakeLauncher):
        def __call__(self, argv: list[str], log_path: Path) -> FakeProc:
            state_file = Path(argv[argv.index("--state-file") + 1])
            state_file.write_text(json.dumps({"run_id": "run-xyz", "status": "failed"}))
            return super().__call__(argv, log_path)

    launcher = ResultWritingLauncher(exit_codes={"failing job": 1})

    report = serve_queue(
        queue,
        once=True,
        launcher=launcher,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert report.failed == [job.job_id]
    stored = QueueJob.model_validate_json(
        (queue / FAILED_DIR / f"{job.job_id}.json").read_text()
    )
    assert stored.exit_code == 1
    assert stored.run_id == "run-xyz"
    assert stored.run_status == "failed"


def test_serve_launch_failure_moves_job_to_failed(tmp_path: Path):
    queue = tmp_path / "queue"
    job = _job()
    enqueue_job(queue, job)

    def exploding_launcher(argv: list[str], log_path: Path) -> FakeProc:
        raise OSError("no such interpreter")

    report = serve_queue(
        queue,
        once=True,
        launcher=exploding_launcher,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert report.failed == [job.job_id]
    stored = QueueJob.model_validate_json(
        (queue / FAILED_DIR / f"{job.job_id}.json").read_text()
    )
    assert "Failed to launch run" in (stored.error or "")


def test_serve_requeues_orphaned_running_jobs_then_processes_them(tmp_path: Path):
    queue = tmp_path / "queue"
    job = _job()
    enqueue_job(queue, job)
    claim_next_job(queue)  # simulate a serve that crashed mid-run
    launcher = FakeLauncher()

    report = serve_queue(
        queue,
        once=True,
        launcher=launcher,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert report.requeued == [job.job_id]
    assert report.done == [job.job_id]


def test_serve_interrupt_waits_for_running_jobs(tmp_path: Path):
    queue = tmp_path / "queue"
    running_job = new_job("in flight", "/tmp/t", now=NOW, uuid_hex="aa")
    never_started = new_job("never started", "/tmp/t", now=NOW, uuid_hex="bb")
    enqueue_job(queue, running_job)
    enqueue_job(queue, never_started)
    launcher = FakeLauncher()

    interrupts = iter([KeyboardInterrupt(), None, None, None])

    def interrupting_sleep(_seconds: float) -> None:
        signal = next(interrupts)
        if signal is not None:
            raise signal

    report = serve_queue(
        queue,
        once=False,  # daemon mode: only the interrupt stops it
        launcher=launcher,
        sleep_fn=interrupting_sleep,
        now_fn=lambda: NOW,
    )

    # The in-flight job was finished and recorded; the second was never claimed.
    assert report.done == [running_job.job_id]
    assert (queue / PENDING_DIR / f"{never_started.job_id}.json").exists()


def test_serve_rejects_non_positive_max_concurrent(tmp_path: Path):
    with pytest.raises(ValueError, match="max_concurrent"):
        serve_queue(tmp_path / "queue", max_concurrent=0)


# --- real subprocess launcher (offline: plain python child processes) ------------


def test_launch_run_subprocess_plumbing_with_real_child(tmp_path: Path):
    queue = tmp_path / "queue"
    job = _job("real child")
    enqueue_job(queue, job)

    def python_argv_builder(job: QueueJob, *, runs_dir, state_file) -> list[str]:
        script = (
            "import json, pathlib, sys; "
            f"pathlib.Path({str(state_file)!r}).write_text("
            "json.dumps({'run_id': 'run-real', 'status': 'completed'})); "
            "print('child ran'); sys.exit(0)"
        )
        return [sys.executable, "-c", script]

    report = serve_queue(
        queue,
        once=True,
        launcher=launch_run_subprocess,
        argv_builder=python_argv_builder,
        sleep_fn=lambda _s: None,
        now_fn=lambda: NOW,
    )

    assert report.done == [job.job_id]
    stored = QueueJob.model_validate_json(
        (queue / DONE_DIR / f"{job.job_id}.json").read_text()
    )
    assert stored.run_id == "run-real"
    assert stored.run_status == "completed"
    log_text = (queue / "logs" / f"{job.job_id}.log").read_text()
    assert "child ran" in log_text
    assert (queue / RESULTS_DIR / f"{job.job_id}.state.json").exists()


# --- CLI wiring -------------------------------------------------------------------


def test_cli_enqueue_then_serve_once(tmp_path: Path, monkeypatch, capsys):
    import logging

    from crewai_headless_flow import cli
    from crewai_headless_flow import job_queue as job_queue_module

    # Keep --format json output parseable regardless of test order: a
    # pre-existing handler stops _configure_logging from binding this
    # test's captured stdout.
    logging.getLogger("crewai_headless_flow").addHandler(logging.NullHandler())

    target = tmp_path / "target"
    target.mkdir()
    queue = tmp_path / "queue"

    exit_code = cli.main(
        [
            "enqueue",
            "--request",
            "add auth",
            "--target-repo",
            str(target),
            "--queue-dir",
            str(queue),
            "--override-verify",
            "mode=advisory",
            "--format",
            "json",
        ]
    )
    assert exit_code == 0
    enqueue_out = json.loads(capsys.readouterr().out)
    job_file = Path(enqueue_out["path"])
    stored = QueueJob.model_validate_json(job_file.read_text())
    assert stored.request == "add auth"
    assert stored.overrides == {"verify": ["mode=advisory"]}

    launcher = FakeLauncher()
    monkeypatch.setattr(job_queue_module, "launch_run_subprocess", launcher)

    exit_code = cli.main(
        [
            "serve",
            "--queue-dir",
            str(queue),
            "--once",
            "--poll-interval",
            "0.01",
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    serve_out = json.loads(capsys.readouterr().out)
    assert serve_out["processed"] == 1
    assert serve_out["done"] == [stored.job_id]
    assert launcher.launched  # the run CLI was invoked for the job
    argv = launcher.launched[0]
    assert argv[:4] == [sys.executable, "-m", "crewai_headless_flow", "run"]
    assert "--override-verify" in argv


def test_cli_enqueue_rejects_missing_target_repo(tmp_path: Path, capsys):
    from crewai_headless_flow import cli

    exit_code = cli.main(
        [
            "enqueue",
            "--request",
            "x",
            "--target-repo",
            str(tmp_path / "missing"),
            "--queue-dir",
            str(tmp_path / "queue"),
        ]
    )

    assert exit_code == 1
    assert "not a directory" in capsys.readouterr().err
