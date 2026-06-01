"""
Milestone 6: Human-in-the-Loop (HITL) tests.

Verifies the config-gated manual prompt fallback works.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from crewai_headless_flow.config import FlowConfig
from crewai_headless_flow.flow import CrewAIHeadlessFlow
from crewai_headless_flow.state import FlowState


pytestmark = pytest.mark.offline


def _hitl_config(**human_feedback):
    return FlowConfig(
        skills={
            "do_work": "incremental-implementation",
            "finalize": "documentation-and-adrs",
        },
        workers={
            "do_work": {"worker": "grok", "model": "grok-3-latest"},
            "finalize": {"worker": "codex"},
        },
        defaults={"worker": "codex", "timeout": 300},
        human_feedback=human_feedback,
    )


def test_hitl_disabled_by_default_does_not_prompt():
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"stages": {"do_work": {"worker": "codex"}}},
        defaults={},
        human_feedback={"enabled": False},
    )

    flow = CrewAIHeadlessFlow(config=cfg)

    # Should return True immediately without calling input
    with patch("builtins.input") as mock_input:
        result = flow._maybe_ask_human("do_work", "Test message")
        assert result is True
        mock_input.assert_not_called()


def test_hitl_enabled_calls_input_and_respects_answer():
    cfg = _hitl_config(enabled=True, before_do_work=True)

    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", return_value="y"):
        result = flow._maybe_ask_human("do_work", "About to do expensive work")
        assert result is True

    with patch("builtins.input", return_value="yes"):
        result = flow._maybe_ask_human("do_work", "About to do expensive work")
        assert result is True

    with patch("builtins.input", return_value="n"):
        result = flow._maybe_ask_human("do_work", "About to do expensive work")
        assert result is False

    with patch("builtins.input", return_value=""):
        result = flow._maybe_ask_human("do_work", "About to do expensive work")
        assert result is False


def test_hitl_gate_specific_disable_skips_prompt():
    cfg = _hitl_config(enabled=True, before_do_work=False)
    flow = CrewAIHeadlessFlow(config=cfg)

    with patch("builtins.input") as mock_input:
        result = flow._maybe_ask_human("do_work", "About to do expensive work")

    assert result is True
    mock_input.assert_not_called()


def test_hitl_eof_and_keyboard_interrupt_abort():
    cfg = _hitl_config(enabled=True, before_do_work=True)
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", side_effect=EOFError):
        assert flow._maybe_ask_human("do_work", "About to do expensive work") is False

    with patch("builtins.input", side_effect=KeyboardInterrupt):
        assert flow._maybe_ask_human("do_work", "About to do expensive work") is False


def test_hitl_prompt_includes_stage_context(capsys):
    cfg = _hitl_config(enabled=True, before_do_work=True)
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", return_value="y"):
        assert flow._maybe_ask_human("do_work", "About to do expensive work") is True

    out = capsys.readouterr().out
    assert "Stage: do_work" in out
    assert "Can mutate files: yes" in out
    assert "Worker: grok" in out
    assert "Skill: incremental-implementation" in out
    assert "Target repo: /tmp/fake" in out
    assert "Default: no" in out


def test_do_work_human_abort_sets_terminal_state():
    cfg = _hitl_config(enabled=True, before_do_work=True)
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", return_value="n"):
        result = flow.do_work("plan output")

    assert result == "aborted-by-human"
    assert flow.state.status == "aborted_by_human"
    assert flow.state.aborted_stage == "do_work"
    assert flow.state.last_stage == "do_work"
    assert flow.state.errors == ["Aborted by human before do_work"]


def test_finalize_human_abort_sets_terminal_state():
    cfg = _hitl_config(enabled=True, before_finalize=True)
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", return_value="n"):
        result = flow.finalize("pass")

    assert result == "aborted-by-human"
    assert flow.state.status == "aborted_by_human"
    assert flow.state.aborted_stage == "finalize"
    assert flow.state.last_stage == "finalize"
    assert flow.state.errors == ["Aborted by human before finalize"]
