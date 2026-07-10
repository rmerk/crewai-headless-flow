"""
Gap 4 (autonomy Phase 1): escalation channel seam.

The contract is ask(request) -> str | None: the raw answer string, or None
meaning "no answer available" — which routes into the exact path EOFError
took before (record no-input, mark aborted, park resumably).
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from crewai_headless_flow.config import FlowConfig
from crewai_headless_flow.escalation import (
    ANSWERED_APPROVAL_FILENAME,
    PENDING_APPROVAL_FILENAME,
    CommandEscalationHandler,
    EscalationRequest,
    FileEscalationHandler,
    StdinEscalationHandler,
    get_handler,
)
from crewai_headless_flow.flow import CrewAIHeadlessFlow
from crewai_headless_flow.state import FlowState
from crewai_headless_flow.workers.base import CoderResult


pytestmark = pytest.mark.offline


REQUEST = EscalationRequest(
    stage="do_work",
    gate="before_do_work",
    prompt="[Human Feedback] Proceed with do_work?",
    run_id="20260710-153000-add-auth-3fa2b1cd",
    run_dir="/tmp/runs/20260710-153000-add-auth-3fa2b1cd",
    target_repo="/tmp/repo",
    revisions=1,
)


# --- handler selection -------------------------------------------------------


def test_get_handler_defaults_to_stdin():
    handler = get_handler({})

    assert isinstance(handler, StdinEscalationHandler)


def test_get_handler_builds_file_channel_with_run_dir(tmp_path: Path):
    hf = {"escalation": {"channel": "file"}}

    handler = get_handler(hf, run_dir=tmp_path)

    assert isinstance(handler, FileEscalationHandler)
    assert handler.run_dir == tmp_path


def test_get_handler_builds_command_channel():
    hf = {
        "escalation": {
            "channel": "command",
            "command": ["notify-hook", "--gate"],
            "timeout_seconds": 60,
            "on_timeout": "proceed",
        }
    }

    handler = get_handler(hf)

    assert isinstance(handler, CommandEscalationHandler)
    assert handler.argv == ["notify-hook", "--gate"]
    assert handler.timeout_seconds == 60
    assert handler.on_timeout == "proceed"


# --- stdin channel ------------------------------------------------------------


def test_stdin_handler_returns_stripped_answer():
    with patch("builtins.input", return_value="  y  "):
        assert StdinEscalationHandler().ask(REQUEST) == "y"


def test_stdin_handler_returns_none_on_eof():
    with patch("builtins.input", side_effect=EOFError):
        assert StdinEscalationHandler().ask(REQUEST) is None


def test_stdin_handler_returns_none_on_keyboard_interrupt():
    with patch("builtins.input", side_effect=KeyboardInterrupt):
        assert StdinEscalationHandler().ask(REQUEST) is None


# --- file channel --------------------------------------------------------------


def test_file_handler_writes_pending_approval_and_parks(tmp_path: Path):
    handler = FileEscalationHandler(tmp_path)

    answer = handler.ask(REQUEST)

    assert answer is None
    payload = json.loads((tmp_path / PENDING_APPROVAL_FILENAME).read_text())
    assert payload["stage"] == "do_work"
    assert payload["gate"] == "before_do_work"
    assert payload["prompt"] == REQUEST.prompt
    assert payload["run_id"] == REQUEST.run_id
    assert "answer" in payload["how_to_answer"]
    assert "--resume-state-file" in payload["how_to_answer"]


def test_file_handler_consumes_operator_answer_and_renames(tmp_path: Path):
    pending = tmp_path / PENDING_APPROVAL_FILENAME
    pending.write_text(json.dumps({"stage": "do_work", "answer": "y"}))
    handler = FileEscalationHandler(tmp_path)

    answer = handler.ask(REQUEST)

    assert answer == "y"
    assert not pending.exists()
    assert (tmp_path / ANSWERED_APPROVAL_FILENAME).exists()


def test_file_handler_ignores_blank_answer_and_rewrites_request(tmp_path: Path):
    pending = tmp_path / PENDING_APPROVAL_FILENAME
    pending.write_text(json.dumps({"answer": "   "}))
    handler = FileEscalationHandler(tmp_path)

    answer = handler.ask(REQUEST)

    assert answer is None
    assert pending.exists()  # rewritten with the fresh request
    assert not (tmp_path / ANSWERED_APPROVAL_FILENAME).exists()


def test_file_handler_without_run_dir_parks_without_artifact(
    caplog, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(logging.getLogger("crewai_headless_flow"), "propagate", True)
    handler = FileEscalationHandler(None)

    with caplog.at_level("WARNING", logger="crewai_headless_flow.escalation"):
        answer = handler.ask(REQUEST)

    assert answer is None
    assert "no run directory" in caplog.text


# --- command channel ------------------------------------------------------------


def _fake_completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["hook"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_command_handler_pipes_request_json_and_parses_first_line():
    seen: dict = {}

    def runner(argv, **kwargs):
        seen["argv"] = argv
        seen["stdin"] = kwargs["input"]
        seen["timeout"] = kwargs["timeout"]
        return _fake_completed(stdout="\n  y \nextra noise\n")

    handler = CommandEscalationHandler(
        ["notify-hook"], timeout_seconds=60, runner=runner
    )

    answer = handler.ask(REQUEST)

    assert answer == "y"
    assert seen["argv"] == ["notify-hook"]
    assert seen["timeout"] == 60
    piped = json.loads(seen["stdin"])
    assert piped["stage"] == "do_work"
    assert piped["run_id"] == REQUEST.run_id


def test_command_handler_timeout_aborts_by_default():
    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

    handler = CommandEscalationHandler(["hook"], timeout_seconds=1, runner=runner)

    assert handler.ask(REQUEST) is None


def test_command_handler_timeout_can_proceed():
    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

    handler = CommandEscalationHandler(
        ["hook"], timeout_seconds=1, on_timeout="proceed", runner=runner
    )

    assert handler.ask(REQUEST) == "y"


def test_command_handler_nonzero_exit_parks():
    handler = CommandEscalationHandler(
        ["hook"], runner=lambda argv, **kw: _fake_completed(returncode=3)
    )

    assert handler.ask(REQUEST) is None


def test_command_handler_launch_failure_parks():
    def runner(argv, **kwargs):
        raise OSError("no such file")

    handler = CommandEscalationHandler(["missing-hook"], runner=runner)

    assert handler.ask(REQUEST) is None


def test_command_handler_empty_stdout_parks():
    handler = CommandEscalationHandler(
        ["hook"], runner=lambda argv, **kw: _fake_completed(stdout="\n\n")
    )

    assert handler.ask(REQUEST) is None


# --- flow integration -----------------------------------------------------------


class FakeHandler:
    def __init__(self, answer: str | None):
        self.answer = answer
        self.requests: list[EscalationRequest] = []

    def ask(self, request: EscalationRequest) -> str | None:
        self.requests.append(request)
        return self.answer


class GateStubWorker:
    def run(self, **kwargs) -> CoderResult:
        return CoderResult(summary="work done", raw_output="work done", exit_code=0)


def _gated_flow(tmp_path: Path, handler: FakeHandler) -> CrewAIHeadlessFlow:
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"do_work": {"worker": "claude"}},
        defaults={"worker": "codex", "timeout": 300},
        human_feedback={"enabled": True, "before_do_work": True},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="gated work",
        target_repo=str(tmp_path / "repo"),
        run_id="run-1",
        run_dir=str(tmp_path / "runs" / "run-1"),
    )
    flow._workers["do_work"] = GateStubWorker()  # type: ignore
    flow._escalation = handler  # type: ignore[assignment]
    return flow


def test_flow_parks_resumably_when_handler_returns_none(tmp_path: Path):
    handler = FakeHandler(None)
    flow = _gated_flow(tmp_path, handler)

    cast(Any, flow).do_work("plan output")

    assert flow.state.status == "aborted_by_human"
    assert flow.state.aborted_checkpoint is not None
    assert flow.state.aborted_checkpoint.stage == "do_work"
    entry = flow.state.human_feedback_log[-1]
    assert entry.response == "no-input"
    assert entry.approved is False
    request = handler.requests[0]
    assert request.stage == "do_work"
    assert request.gate == "before_do_work"
    assert request.run_id == "run-1"
    assert "Proceed" in request.prompt or request.prompt


def test_flow_proceeds_when_handler_answers_yes(tmp_path: Path):
    handler = FakeHandler("y")
    flow = _gated_flow(tmp_path, handler)

    result = cast(Any, flow).do_work("plan output")

    assert flow.state.status != "aborted_by_human"
    assert result == "work done"
    entry = flow.state.human_feedback_log[-1]
    assert entry.approved is True


# --- end-to-end file channel: park -> operator answers -> resume ----------------


class AllStagesStubWorker:
    def run(self, **kwargs) -> CoderResult:
        if kwargs.get("mode") == "inspect":
            return CoderResult(
                summary="review complete",
                raw_output='{"status": "pass", "issues": [], "summary": "ok"}',
                exit_code=0,
            )
        return CoderResult(summary="work done", raw_output="work done", exit_code=0)


def _file_channel_config() -> FlowConfig:
    return FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
            "review": "code-review-and-quality",
            "finalize": "documentation-and-adrs",
        },
        workers={stage: {"worker": "claude"} for stage in ("plan", "do_work")}
        | {
            "review": {"worker": "claude", "sandbox": "read-only"},
            "finalize": {"worker": "claude"},
        },
        defaults={"worker": "codex", "timeout": 300},
        human_feedback={
            "enabled": True,
            "before_plan": False,
            "before_do_work": True,
            "before_review": False,
            "after_review": False,
            "before_finalize": False,
            "escalation": {"channel": "file"},
        },
    )


def test_file_channel_parks_then_resume_consumes_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from crewai_headless_flow import flow as flow_module
    from crewai_headless_flow.run_store import RunStore

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _file_channel_config()
    store = RunStore.allocate(tmp_path / "runs", "file channel e2e")

    # 1. Unattended run reaches the before_do_work gate and parks.
    flow = CrewAIHeadlessFlow(config=cfg, run_store=store)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="file channel e2e",
        target_repo=str(repo),
        run_id=store.run_id,
        run_dir=str(store.run_dir),
    )
    flow._workers = {  # type: ignore[assignment]
        stage: AllStagesStubWorker()  # type: ignore[misc]
        for stage in flow._workers
    }

    cast(Any, flow).do_work("plan output")

    assert flow.state.status == "aborted_by_human"
    pending = store.run_dir / PENDING_APPROVAL_FILENAME
    assert pending.exists()
    # The crash-safe checkpoint already holds the parked state on disk.
    saved = json.loads(store.state_path.read_text())
    assert saved["status"] == "aborted_by_human"

    # 2. The operator answers by editing the approval file.
    payload = json.loads(pending.read_text())
    payload["answer"] = "y"
    pending.write_text(json.dumps(payload))

    # 3. Resume replays the gate; the handler consumes the answer.
    class StubbedFlow(CrewAIHeadlessFlow):
        def __init__(self, config=None):
            super().__init__(config=config)
            self._workers = {  # type: ignore[assignment]
                stage: AllStagesStubWorker() for stage in self._workers
            }

    monkeypatch.setattr(flow_module, "CrewAIHeadlessFlow", StubbedFlow)
    parked_state = FlowState.model_validate(saved)

    resumed = flow_module.resume_headless_flow(parked_state, config=cfg)

    assert resumed.status == "completed"
    assert not pending.exists()
    assert (store.run_dir / ANSWERED_APPROVAL_FILENAME).exists()
    approvals = [e for e in resumed.human_feedback_log if e.approved]
    assert approvals, "resume must record the consumed approval"
