"""Objective verification gate (autonomy Gap 1).

Runs operator-declared commands (tests, lint, build) against the target
repo so "review passed" can mean something objective instead of "the
worker said so plus an LLM agreed". The Flow calls this at the top of
every review round; ``mode: gate`` failures skip the LLM review entirely
and feed the command output into the revise loop as concrete evidence,
while ``mode: advisory`` failures are appended to the review prompt.

Design notes:
- This is a Flow-owned subprocess boundary. Commands are never routed
  through a worker adapter, so the inspect/edit safety boundary is
  untouched.
- Commands run as argv (``shlex.split`` for strings) with no shell.
  Shell constructs (pipes, ``&&``) require wrapping in a script.
- ``run_verification`` never raises: timeouts map to exit code 124 and
  launch failures to 127, mirroring coreutils/shell conventions.
- ``runner`` is the single injectable subprocess boundary so the module
  is fully offline-testable (same pattern as ``delivery.run_git``).
"""

from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence, Union

from pydantic import BaseModel, Field

VerifyRunner = Callable[..., "subprocess.CompletedProcess[str]"]

CommandSpec = Union[str, Sequence[str]]

OUTPUT_TAIL_LIMIT = 2000
TIMEOUT_EXIT_CODE = 124
LAUNCH_FAILURE_EXIT_CODE = 127


class VerificationCommandResult(BaseModel):
    """Outcome of one verification command."""

    command: str
    exit_code: int
    output_tail: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False


class VerificationReport(BaseModel):
    """Outcome of one verification round (all commands, fail-fast)."""

    passed: bool
    mode: Literal["gate", "advisory"] = "gate"
    revision: int = 0
    results: list[VerificationCommandResult] = Field(default_factory=list)
    message: str = ""


def _as_argv(command: CommandSpec) -> list[str]:
    if isinstance(command, str):
        return shlex.split(command)
    return [str(part) for part in command]


def _display_command(command: CommandSpec) -> str:
    if isinstance(command, str):
        return command
    return shlex.join(str(part) for part in command)


def _tail(stdout: Any, stderr: Any) -> str:
    combined = "\n".join(
        part.strip() for part in (stdout or "", stderr or "") if part and part.strip()
    )
    return combined[-OUTPUT_TAIL_LIMIT:]


def run_verification(
    cfg: Mapping[str, Any],
    cwd: Path | str,
    *,
    runner: VerifyRunner = subprocess.run,
) -> VerificationReport:
    """Run the configured verification commands in ``cwd``, fail-fast."""

    commands: list[CommandSpec] = list(cfg.get("commands") or [])
    mode = cfg.get("mode", "gate")
    timeout = cfg.get("timeout", 600)

    report = VerificationReport(passed=True, mode=mode)
    if not commands:
        report.message = "No verification commands configured."
        return report

    for command in commands:
        display = _display_command(command)
        started = time.monotonic()
        try:
            proc = runner(
                _as_argv(command),
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            result = VerificationCommandResult(
                command=display,
                exit_code=proc.returncode,
                output_tail=_tail(proc.stdout, proc.stderr),
                duration_seconds=time.monotonic() - started,
            )
        except subprocess.TimeoutExpired as exc:
            result = VerificationCommandResult(
                command=display,
                exit_code=TIMEOUT_EXIT_CODE,
                output_tail=_tail(exc.stdout, exc.stderr),
                duration_seconds=time.monotonic() - started,
                timed_out=True,
            )
        except OSError as exc:
            result = VerificationCommandResult(
                command=display,
                exit_code=LAUNCH_FAILURE_EXIT_CODE,
                output_tail=f"Failed to launch: {exc}",
                duration_seconds=time.monotonic() - started,
            )

        report.results.append(result)
        if result.exit_code != 0:
            report.passed = False
            suffix = " (timed out)" if result.timed_out else ""
            report.message = f"`{display}` exited {result.exit_code}{suffix}"
            return report

    report.message = f"{len(report.results)}/{len(commands)} commands passed."
    return report
