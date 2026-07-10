"""
Escalation channel seam (autonomy Phase 1, Gap 4).

When a human-feedback gate fires, the Flow needs an answer from an operator.
Historically that was a blocking stdin ``input()`` — which, in an unattended
(no-TTY) run, degrades to ``EOFError`` and aborts the entire flow. This seam
makes "how the question reaches a human" pluggable while leaving every piece
of action parsing in ``flow.py`` untouched.

The contract is deliberately tiny::

    handler.ask(request) -> str | None

The raw answer string is fed to the Flow's existing parsing; ``None`` means
"no answer available" and routes into the exact path ``EOFError`` takes
today — record a ``no-input`` feedback entry, mark the run aborted, and park
it resumably via the aborted-checkpoint machinery.

Channels:

- ``stdin`` (default): today's behavior, byte for byte.
- ``file``: write ``pending_approval.json`` into the run dir and park. The
  operator answers by adding an ``"answer"`` field to the file and re-running
  with ``--resume-state-file``; resume replays the gate and the handler
  consumes the answer (renaming the file to ``answered_approval.json``). No
  polling, no long-lived process.
- ``command``: run a configured argv with the request JSON on stdin and parse
  the answer from the first non-empty stdout line. This is where Slack /
  email / webhook notification plugs in without the platform growing any
  network code.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol

PENDING_APPROVAL_FILENAME = "pending_approval.json"
ANSWERED_APPROVAL_FILENAME = "answered_approval.json"

# Channel/enum values and defaults are owned by config.py's
# _validate_escalation; this module only consumes validated config.

_PROMPT = "Proceed? [y/N]: "

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class EscalationRequest:
    """Everything an out-of-band operator needs to answer a gate."""

    stage: str
    gate: str
    prompt: str
    run_id: str | None = None
    run_dir: str | None = None
    target_repo: str = ""
    revisions: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


class EscalationHandler(Protocol):
    def ask(self, request: EscalationRequest) -> Optional[str]: ...


class StdinEscalationHandler:
    """Blocking stdin prompt — the pre-seam behavior."""

    def ask(self, request: EscalationRequest) -> Optional[str]:
        try:
            # Look up the builtin at call time so tests patching
            # builtins.input keep working.
            return input(_PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            return None


class FileEscalationHandler:
    """Park-and-resume approval via a JSON file in the run directory."""

    def __init__(self, run_dir: Path | None) -> None:
        self.run_dir = Path(run_dir) if run_dir is not None else None

    def ask(self, request: EscalationRequest) -> Optional[str]:
        if self.run_dir is None:
            print(
                "[Escalation] file channel configured but no run directory is "
                "available (--runs-dir disabled?); parking without an "
                "approval file."
            )
            return None

        pending = self.run_dir / PENDING_APPROVAL_FILENAME
        answer = self._consume_answer(pending)
        if answer is not None:
            return answer

        payload = json.loads(request.to_json())
        payload["how_to_answer"] = (
            'Set an "answer" field in this file (e.g. "y" to approve, "n" to '
            "abort) and re-run with --resume-state-file "
            f"{self.run_dir / 'state.json'}"
        )
        pending.write_text(json.dumps(payload, indent=2, sort_keys=True))
        print(f"[Escalation] Approval request written to {pending}; parking run.")
        return None

    def _consume_answer(self, pending: Path) -> Optional[str]:
        if not pending.exists():
            return None
        try:
            data = json.loads(pending.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        answer = data.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            return None
        pending.replace(pending.parent / ANSWERED_APPROVAL_FILENAME)
        print(f"[Escalation] Consumed operator answer from {pending.name}.")
        return answer.strip()


class CommandEscalationHandler:
    """Delegate the question to a configured hook command.

    The request JSON goes to the command's stdin; the answer is the first
    non-empty stdout line. Timeouts and non-zero exits degrade to the
    configured fallback instead of raising.
    """

    def __init__(
        self,
        argv: list[str],
        timeout_seconds: int = 300,
        on_timeout: str = "abort",
        runner: Runner = subprocess.run,
    ) -> None:
        self.argv = argv
        self.timeout_seconds = timeout_seconds
        self.on_timeout = on_timeout
        self._runner = runner

    def ask(self, request: EscalationRequest) -> Optional[str]:
        try:
            proc = self._runner(
                self.argv,
                input=request.to_json(),
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            print(
                f"[Escalation] Command timed out after {self.timeout_seconds}s; "
                f"on_timeout={self.on_timeout}."
            )
            return "y" if self.on_timeout == "proceed" else None
        except Exception as exc:
            print(f"[Escalation] Command failed to run: {exc}; parking run.")
            return None

        if proc.returncode != 0:
            print(
                f"[Escalation] Command exited {proc.returncode}; parking run. "
                f"stderr: {(proc.stderr or '')[:500]}"
            )
            return None

        for line in (proc.stdout or "").splitlines():
            if line.strip():
                return line.strip()
        return None


def get_handler(
    hf_config: Mapping[str, object],
    *,
    run_dir: str | Path | None = None,
    runner: Runner = subprocess.run,
) -> EscalationHandler:
    """Build the configured escalation handler from human_feedback config."""
    escalation = hf_config.get("escalation")
    escalation = escalation if isinstance(escalation, Mapping) else {}
    channel = escalation.get("channel", "stdin")

    if channel == "file":
        return FileEscalationHandler(Path(run_dir) if run_dir else None)
    if channel == "command":
        argv = escalation.get("command") or []
        timeout_seconds = escalation.get("timeout_seconds", 300)
        on_timeout = escalation.get("on_timeout", "abort")
        return CommandEscalationHandler(
            argv=[str(part) for part in argv],
            timeout_seconds=int(timeout_seconds),  # type: ignore[arg-type]
            on_timeout=str(on_timeout),
            runner=runner,
        )
    return StdinEscalationHandler()
