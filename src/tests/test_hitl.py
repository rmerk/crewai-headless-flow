"""
Milestone 6: Human-in-the-Loop (HITL) tests.

Verifies the config-gated manual prompt fallback works.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from crewai_headless_flow.config import FlowConfig
from crewai_headless_flow.flow import CrewAIHeadlessFlow


pytestmark = pytest.mark.offline


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
    cfg = FlowConfig(
        skills={"do_work": "incremental-implementation"},
        workers={"stages": {"do_work": {"worker": "codex"}}},
        defaults={},
        human_feedback={"enabled": True, "before_do_work": True},
    )

    flow = CrewAIHeadlessFlow(config=cfg)

    with patch("builtins.input", return_value="y"):
        result = flow._maybe_ask_human("do_work", "About to do expensive work")
        assert result is True

    with patch("builtins.input", return_value="n"):
        result = flow._maybe_ask_human("do_work", "About to do expensive work")
        assert result is False
