"""Offline tests for the objective verification gate (verification.py)."""

from __future__ import annotations

import subprocess

import pytest

from crewai_headless_flow.verification import (
    LAUNCH_FAILURE_EXIT_CODE,
    OUTPUT_TAIL_LIMIT,
    TIMEOUT_EXIT_CODE,
    run_verification,
)


pytestmark = pytest.mark.offline


class FakeRunner:
    """Scripted subprocess.run stand-in keyed by argv tuple."""

    def __init__(
        self, outcomes: dict[tuple[str, ...], tuple[int, str, str]] | None = None
    ):
        self.outcomes = outcomes or {}
        self.calls: list[dict] = []

    def __call__(self, argv, *, cwd, capture_output, text, timeout, check):
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
        exit_code, stdout, stderr = self.outcomes.get(tuple(argv), (0, "ok", ""))
        return subprocess.CompletedProcess(argv, exit_code, stdout, stderr)


def _cfg(**overrides) -> dict:
    cfg = {"commands": [], "mode": "gate", "timeout": 600}
    cfg.update(overrides)
    return cfg


def test_all_commands_pass():
    runner = FakeRunner()
    report = run_verification(
        _cfg(commands=["pytest -q", "ruff check ."]), cwd="/tmp/r", runner=runner
    )

    assert report.passed is True
    assert report.mode == "gate"
    assert [r.exit_code for r in report.results] == [0, 0]
    assert report.message == "2/2 commands passed."
    assert runner.calls[0]["argv"] == ["pytest", "-q"]
    assert runner.calls[1]["argv"] == ["ruff", "check", "."]
    assert all(call["cwd"] == "/tmp/r" for call in runner.calls)


def test_fail_fast_stops_at_first_failure():
    runner = FakeRunner(outcomes={("pytest", "-q"): (1, "1 failed, 3 passed", "boom")})
    report = run_verification(
        _cfg(commands=["pytest -q", "ruff check ."]), cwd="/tmp/r", runner=runner
    )

    assert report.passed is False
    assert len(report.results) == 1
    assert len(runner.calls) == 1  # ruff never ran
    assert report.results[0].exit_code == 1
    assert "1 failed" in report.results[0].output_tail
    assert "boom" in report.results[0].output_tail
    assert report.message == "`pytest -q` exited 1"


def test_timeout_maps_to_exit_124():
    # TimeoutExpired.stdout/.stderr are raw BYTES even when subprocess.run
    # was invoked with text=True — the fake must match reality or _tail's
    # decode path goes untested.
    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(
            argv, kwargs["timeout"], output=b"partial out", stderr=b"late stderr"
        )

    report = run_verification(
        _cfg(commands=["pytest -q"], timeout=5), cwd="/tmp/r", runner=runner
    )

    assert report.passed is False
    result = report.results[0]
    assert result.exit_code == TIMEOUT_EXIT_CODE
    assert result.timed_out is True
    assert "partial out" in result.output_tail
    assert "late stderr" in result.output_tail
    assert "(timed out)" in report.message


def test_timeout_with_undecodable_bytes_never_raises():
    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(
            argv, kwargs["timeout"], output=b"partial \xff out"
        )

    report = run_verification(
        _cfg(commands=["pytest -q"], timeout=5), cwd="/tmp/r", runner=runner
    )

    assert report.passed is False
    assert "partial" in report.results[0].output_tail
    assert "out" in report.results[0].output_tail


def test_launch_failure_maps_to_exit_127_and_never_raises():
    def runner(argv, **kwargs):
        raise OSError("No such file or directory: 'nonexistent'")

    report = run_verification(
        _cfg(commands=["nonexistent --flag"]), cwd="/tmp/r", runner=runner
    )

    assert report.passed is False
    result = report.results[0]
    assert result.exit_code == LAUNCH_FAILURE_EXIT_CODE
    assert "Failed to launch" in result.output_tail


def test_unparseable_string_command_maps_to_exit_127_and_never_raises():
    runner = FakeRunner()
    report = run_verification(
        _cfg(commands=["pytest 'unclosed"]), cwd="/tmp/r", runner=runner
    )

    assert report.passed is False
    assert runner.calls == []  # never launched
    result = report.results[0]
    assert result.exit_code == LAUNCH_FAILURE_EXIT_CODE
    assert "Failed to parse command" in result.output_tail
    assert f"exited {LAUNCH_FAILURE_EXIT_CODE}" in report.message


def test_output_tail_is_truncated():
    runner = FakeRunner(outcomes={("noisy",): (1, "x" * 10_000, "")})
    report = run_verification(_cfg(commands=["noisy"]), cwd="/tmp/r", runner=runner)

    assert len(report.results[0].output_tail) == OUTPUT_TAIL_LIMIT


def test_list_command_form_is_passed_as_argv_verbatim():
    runner = FakeRunner()
    report = run_verification(
        _cfg(commands=[["uv", "run", "pytest", "-m", "offline"]]),
        cwd="/tmp/r",
        runner=runner,
    )

    assert runner.calls[0]["argv"] == ["uv", "run", "pytest", "-m", "offline"]
    assert report.results[0].command == "uv run pytest -m offline"


def test_string_command_is_shlex_split_not_shell():
    runner = FakeRunner()
    run_verification(
        _cfg(commands=["pytest -k 'a and b'"]), cwd="/tmp/r", runner=runner
    )

    assert runner.calls[0]["argv"] == ["pytest", "-k", "a and b"]


def test_empty_commands_passes_without_running_anything():
    runner = FakeRunner()
    report = run_verification(_cfg(), cwd="/tmp/r", runner=runner)

    assert report.passed is True
    assert report.results == []
    assert runner.calls == []
    assert "No verification commands" in report.message


def test_per_command_timeout_is_threaded_to_runner():
    runner = FakeRunner()
    run_verification(_cfg(commands=["a", "b"], timeout=42), cwd="/tmp/r", runner=runner)

    assert [call["timeout"] for call in runner.calls] == [42, 42]


def test_advisory_mode_is_carried_on_the_report():
    runner = FakeRunner(outcomes={("false",): (1, "", "")})
    report = run_verification(
        _cfg(commands=["false"], mode="advisory"), cwd="/tmp/r", runner=runner
    )

    assert report.mode == "advisory"
    assert report.passed is False
