from __future__ import annotations

import pytest

from crewai_headless_flow.config import (
    DEFAULT_HUMAN_FEEDBACK,
    HUMAN_FEEDBACK_BOOLEAN_KEYS,
)
from crewai_headless_flow.human_feedback_actions import (
    default_human_feedback_gate,
    default_human_feedback_gate_for_stage,
    human_feedback_gate_default_enabled,
    human_feedback_gate_label,
    human_feedback_gate_position,
    human_feedback_gate_stage,
    human_feedback_target_label,
    is_after_review_gate,
    is_default_human_feedback_gate,
    stage_mutates_files,
    supported_flow_stages,
    supported_human_feedback_actions,
    supported_human_feedback_gates,
)


pytestmark = pytest.mark.offline


def test_human_feedback_gate_metadata_matches_config_defaults():
    for gate in supported_human_feedback_gates():
        assert gate in DEFAULT_HUMAN_FEEDBACK
        assert DEFAULT_HUMAN_FEEDBACK[gate] is human_feedback_gate_default_enabled(gate)
        assert gate in HUMAN_FEEDBACK_BOOLEAN_KEYS
        assert human_feedback_gate_label(gate)


def test_human_feedback_stage_metadata_helpers():
    assert supported_flow_stages() == ("plan", "do_work", "review", "finalize")

    assert default_human_feedback_gate("plan") == "before_plan"
    assert default_human_feedback_gate("do_work") == "before_do_work"
    assert default_human_feedback_gate("review") == "before_review"
    assert default_human_feedback_gate("finalize") == "before_finalize"

    assert human_feedback_gate_stage("before_plan") == "plan"
    assert human_feedback_gate_stage("after_review") == "review"
    assert human_feedback_gate_stage("before_finalize") == "finalize"
    assert default_human_feedback_gate_for_stage("review") == "before_review"
    assert default_human_feedback_gate_for_stage("unknown") is None
    assert human_feedback_gate_position("before_review") == "before"
    assert human_feedback_gate_position("after_review") == "after"
    assert human_feedback_gate_position("review") is None
    assert is_default_human_feedback_gate("review", None) is True
    assert is_default_human_feedback_gate("review", "before_review") is True
    assert is_default_human_feedback_gate("review", "after_review") is False
    assert human_feedback_target_label("review", "before_review") == "review"
    assert human_feedback_target_label("review", None) == "review"
    assert (
        human_feedback_target_label(
            "review",
            "before_review",
            include_default_gate=True,
        )
        == "review@before_review"
    )
    assert (
        human_feedback_target_label("review", "after_review") == "review@after_review"
    )
    assert is_after_review_gate("review", "after_review") is True
    assert is_after_review_gate("review", "before_review") is False
    assert is_after_review_gate("finalize", "after_review") is False
    assert "target-tasks" in supported_human_feedback_actions("before_do_work")
    assert "rerun-review" in supported_human_feedback_actions("before_finalize")

    assert stage_mutates_files("plan") is False
    assert stage_mutates_files("do_work") is True
    assert stage_mutates_files("review") is False
    assert stage_mutates_files("finalize") is True
