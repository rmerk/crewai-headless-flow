"""
Milestone 6: Human-in-the-Loop (HITL) tests.

Verifies the config-gated manual prompt fallback works.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import patch

import pytest

from crewai_headless_flow.config import FlowConfig
from crewai_headless_flow.flow import CrewAIHeadlessFlow
from crewai_headless_flow.review_contract import ReviewDecision
from crewai_headless_flow.workers.base import CoderResult
from crewai_headless_flow.state import (
    FlowState,
    RepeatedTaskFailureDetail,
    TaskExecutionEntry,
    TaskItem,
)


pytestmark = pytest.mark.offline


def _hitl_config(**human_feedback):
    return FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
            "review": "code-review-and-quality",
            "finalize": "documentation-and-adrs",
        },
        workers={
            "plan": {"worker": "codex"},
            "do_work": {"worker": "grok", "model": "grok-3-latest"},
            "review": {"worker": "codex", "sandbox": "read-only"},
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
        assert result.proceed is True
        assert result.response == "auto-disabled"
        mock_input.assert_not_called()


def test_hitl_enabled_calls_input_and_respects_answer():
    cfg = _hitl_config(enabled=True, before_do_work=True)

    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", return_value="y"):
        result = flow._maybe_ask_human("do_work", "About to do expensive work")
        assert result.proceed is True

    with patch("builtins.input", return_value="yes"):
        result = flow._maybe_ask_human("do_work", "About to do expensive work")
        assert result.proceed is True

    with patch("builtins.input", return_value="n"):
        result = flow._maybe_ask_human("do_work", "About to do expensive work")
        assert result.proceed is False

    with patch("builtins.input", return_value=""):
        result = flow._maybe_ask_human("do_work", "About to do expensive work")
        assert result.proceed is False


def test_hitl_gate_specific_disable_skips_prompt():
    cfg = _hitl_config(enabled=True, before_do_work=False)
    flow = CrewAIHeadlessFlow(config=cfg)

    with patch("builtins.input") as mock_input:
        result = flow._maybe_ask_human("do_work", "About to do expensive work")

    assert result.proceed is True
    assert result.response == "auto-gate-disabled"
    mock_input.assert_not_called()


def test_hitl_eof_and_keyboard_interrupt_abort():
    cfg = _hitl_config(enabled=True, before_do_work=True)
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", side_effect=EOFError):
        assert (
            flow._maybe_ask_human("do_work", "About to do expensive work").proceed
            is False
        )

    with patch("builtins.input", side_effect=KeyboardInterrupt):
        assert (
            flow._maybe_ask_human("do_work", "About to do expensive work").proceed
            is False
        )


def test_hitl_prompt_includes_stage_context(capsys):
    cfg = _hitl_config(enabled=True, before_do_work=True)
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", return_value="y"):
        assert (
            flow._maybe_ask_human("do_work", "About to do expensive work").proceed
            is True
        )

    out = capsys.readouterr().out
    assert "Stage: do_work" in out
    assert "Gate: before_do_work" in out
    assert "Can mutate files: yes" in out
    assert "Worker: grok" in out
    assert "Skill: incremental-implementation" in out
    assert "Target repo: /tmp/fake" in out
    assert "Default: no" in out
    assert "Options: approve=y/yes | abort=n/no/empty" in out
    assert "Optional instructions after approval: disabled" in out


def test_hitl_capture_instructions_records_audit_entry():
    cfg = _hitl_config(
        enabled=True,
        before_do_work=True,
        capture_instructions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", side_effect=["y", "Focus on tests first"]):
        result = flow._maybe_ask_human("do_work", "About to do expensive work")

    assert result.proceed is True
    assert result.instructions == "Focus on tests first"
    assert flow.state.human_feedback_log[-1].approved is True
    assert flow.state.human_feedback_log[-1].instructions == "Focus on tests first"
    assert flow.state.human_feedback_log[-1].response == "y"
    assert flow.state.human_feedback_log[-1].action == "approve"


def test_hitl_advanced_actions_parse_without_instruction_prompt():
    cfg = _hitl_config(
        enabled=True,
        before_do_work=True,
        advanced_actions=True,
        capture_instructions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", return_value="review") as mock_input:
        result = flow._maybe_ask_human("do_work", "About to do expensive work")

    assert result.proceed is False
    assert result.action == "skip-to-review"
    assert result.instructions is None
    assert mock_input.call_count == 1
    assert flow.state.human_feedback_log[-1].action == "skip-to-review"


def test_hitl_replan_action_captures_replan_reason():
    cfg = _hitl_config(
        enabled=True,
        before_do_work=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", side_effect=["replan", "Split docs from code"]):
        result = flow._maybe_ask_human("do_work", "About to do expensive work")

    assert result.proceed is False
    assert result.action == "replan"
    assert result.instructions == "Split docs from code"
    assert flow.state.human_feedback_log[-1].action == "replan"
    assert flow.state.human_feedback_log[-1].instructions == "Split docs from code"


def test_hitl_do_work_target_tasks_captures_selected_ids_and_execution_note():
    cfg = _hitl_config(
        enabled=True,
        before_do_work=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        tasks=[
            TaskItem(id=1, title="api", description="api task", files=["src/api.py"]),
            TaskItem(
                id=2,
                title="docs",
                description="docs task",
                files=["docs/readme.md"],
            ),
        ],
    )

    with patch(
        "builtins.input",
        side_effect=["target", "2", "Only run docs first"],
    ):
        result = flow._maybe_ask_human("do_work", "About to do expensive work")

    assert result.proceed is False
    assert result.action == "target-tasks"
    assert result.task_ids == [2]
    assert result.instructions == "Only run docs first"
    assert flow.state.human_feedback_log[-1].action == "target-tasks"
    assert flow.state.human_feedback_log[-1].task_ids == [2]
    assert flow.state.human_feedback_log[-1].instructions == "Only run docs first"


def test_hitl_action_allowlist_enables_stage_shortcut_without_global_advanced_actions():
    cfg = _hitl_config(
        enabled=True,
        before_review=True,
        action_allowlist={"review": ["force-pass", "force-revise"]},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", return_value="pass"):
        result = flow._maybe_ask_human("review", "About to review")

    assert result.proceed is False
    assert result.action == "force-pass"
    assert flow.state.human_feedback_log[-1].action == "force-pass"


def test_hitl_gate_scoped_action_allowlist_overrides_stage_scope_per_gate():
    cfg = _hitl_config(
        enabled=True,
        before_review=True,
        after_review=True,
        action_allowlist={
            "review": ["force-pass"],
            "before_review": [],
        },
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", return_value="pass"):
        before_result = flow._maybe_ask_human(
            "review", "About to review", gate="before_review"
        )

    with patch("builtins.input", return_value="pass"):
        after_result = flow._maybe_ask_human(
            "review", "Automated review completed", gate="after_review"
        )

    assert before_result.proceed is False
    assert before_result.action == "abort"
    assert after_result.proceed is False
    assert after_result.action == "force-pass"
    assert flow.state.human_feedback_log[-2].gate == "before_review"
    assert flow.state.human_feedback_log[-2].action == "abort"
    assert flow.state.human_feedback_log[-1].gate == "after_review"
    assert flow.state.human_feedback_log[-1].action == "force-pass"


def test_hitl_force_revise_captures_revision_reason():
    cfg = _hitl_config(
        enabled=True,
        before_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", side_effect=["revise", "Need more tests"]):
        result = flow._maybe_ask_human("review", "About to review")

    assert result.proceed is False
    assert result.action == "force-revise"
    assert result.instructions == "Need more tests"
    assert flow.state.human_feedback_log[-1].action == "force-revise"
    assert flow.state.human_feedback_log[-1].instructions == "Need more tests"


def test_hitl_review_replan_captures_replan_reason_when_tasks_exist():
    cfg = _hitl_config(
        enabled=True,
        before_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow.state.tasks = [TaskItem(id=1, description="Ship feature")]

    with patch("builtins.input", side_effect=["replan", "Split api from docs"]):
        result = flow._maybe_ask_human("review", "About to review")

    assert result.proceed is False
    assert result.action == "replan"
    assert result.instructions == "Split api from docs"
    assert flow.state.human_feedback_log[-1].action == "replan"
    assert flow.state.human_feedback_log[-1].instructions == "Split api from docs"


def test_hitl_review_replan_default_reason_is_gate_specific():
    cfg = _hitl_config(
        enabled=True,
        before_review=True,
        after_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow.state.tasks = [TaskItem(id=1, description="Ship feature")]

    with patch("builtins.input", side_effect=["replan", ""]):
        before_result = flow._maybe_ask_human(
            "review", "About to review", gate="before_review"
        )

    with patch("builtins.input", side_effect=["replan", ""]):
        after_result = flow._maybe_ask_human(
            "review", "Automated review completed", gate="after_review"
        )

    assert before_result.action == "replan"
    assert (
        before_result.instructions
        == "Human requested replanning before automated review."
    )
    assert after_result.action == "replan"
    assert (
        after_result.instructions
        == "Human requested replanning after automated review."
    )


def test_hitl_action_allowlist_can_disable_stage_shortcut_with_global_advanced_actions():
    cfg = _hitl_config(
        enabled=True,
        before_do_work=True,
        advanced_actions=True,
        action_allowlist={"do_work": []},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", return_value="review"):
        result = flow._maybe_ask_human("do_work", "About to do expensive work")

    assert result.proceed is False
    assert result.action == "abort"
    assert flow.state.human_feedback_log[-1].action == "abort"


def test_hitl_replan_action_is_not_available_after_task_execution_begins():
    cfg = _hitl_config(
        enabled=True,
        before_do_work=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow.state.task_executions = [
        TaskExecutionEntry(
            task_id=1,
            attempt=1,
            worker="grok",
            success=True,
            summary="done",
        )
    ]

    with patch("builtins.input", return_value="replan"):
        result = flow._maybe_ask_human("do_work", "About to do expensive work")

    assert result.proceed is False
    assert result.action == "abort"
    assert flow.state.human_feedback_log[-1].action == "abort"


def test_hitl_review_replan_is_not_available_without_structured_tasks():
    cfg = _hitl_config(
        enabled=True,
        before_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", return_value="replan"):
        result = flow._maybe_ask_human("review", "About to review")

    assert result.proceed is False
    assert result.action == "abort"


def test_hitl_review_rerun_is_only_available_after_review():
    cfg = _hitl_config(
        enabled=True,
        before_review=True,
        after_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", return_value="rerun"):
        before_result = flow._maybe_ask_human(
            "review", "About to review", gate="before_review"
        )

    with patch("builtins.input", side_effect=["rerun", "Focus on migrations"]):
        after_result = flow._maybe_ask_human(
            "review", "Automated review completed", gate="after_review"
        )

    assert before_result.proceed is False
    assert before_result.action == "abort"
    assert after_result.proceed is False
    assert after_result.action == "rerun-review"
    assert after_result.instructions == "Focus on migrations"
    assert flow.state.human_feedback_log[-1].gate == "after_review"
    assert flow.state.human_feedback_log[-1].action == "rerun-review"


def test_hitl_review_target_tasks_is_available_before_and_after_review_when_tasks_exist():
    cfg = _hitl_config(
        enabled=True,
        before_review=True,
        after_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, description="one"),
        TaskItem(id=2, description="two"),
    ]

    with patch(
        "builtins.input",
        side_effect=["target", "2,1", "Focus task ordering before review"],
    ):
        before_result = flow._maybe_ask_human(
            "review", "About to review", gate="before_review"
        )

    with patch(
        "builtins.input",
        side_effect=["target", "2,1", "Focus task ordering"],
    ):
        after_result = flow._maybe_ask_human(
            "review", "Automated review completed", gate="after_review"
        )

    assert before_result.proceed is False
    assert before_result.action == "target-tasks"
    assert before_result.task_ids == [2, 1]
    assert before_result.instructions == "Focus task ordering before review"
    assert after_result.proceed is False
    assert after_result.action == "target-tasks"
    assert after_result.task_ids == [2, 1]
    assert after_result.instructions == "Focus task ordering"
    assert flow.state.human_feedback_log[-2].gate == "before_review"
    assert flow.state.human_feedback_log[-2].action == "target-tasks"
    assert flow.state.human_feedback_log[-2].task_ids == [2, 1]
    assert flow.state.human_feedback_log[-1].gate == "after_review"
    assert flow.state.human_feedback_log[-1].action == "target-tasks"
    assert flow.state.human_feedback_log[-1].task_ids == [2, 1]


def test_hitl_review_target_tasks_invalid_ids_fail_closed():
    cfg = _hitl_config(
        enabled=True,
        after_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow.state.tasks = [TaskItem(id=1, description="one")]

    with patch("builtins.input", side_effect=["target", "99"]):
        result = flow._maybe_ask_human(
            "review", "Automated review completed", gate="after_review"
        )

    assert result.proceed is False
    assert result.action == "abort"
    assert result.task_ids is None
    assert flow.state.human_feedback_log[-1].action == "abort"
    assert flow.state.human_feedback_log[-1].task_ids == []


def test_hitl_review_target_tasks_supports_ranges_and_all(capsys):
    cfg = _hitl_config(
        enabled=True,
        after_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="api", description="api task", files=["src/api.py"]),
        TaskItem(
            id=2, title="tests", description="tests task", files=["tests/test_api.py"]
        ),
        TaskItem(id=3, title="docs", description="docs task", files=["docs/readme.md"]),
    ]

    with patch(
        "builtins.input",
        side_effect=["target", "1-2, 3", "Focus all slices"],
    ):
        range_result = flow._maybe_ask_human(
            "review", "Automated review completed", gate="after_review"
        )

    with patch(
        "builtins.input",
        side_effect=["target", "all", "Revisit everything"],
    ):
        all_result = flow._maybe_ask_human(
            "review", "Automated review completed", gate="after_review"
        )

    out = capsys.readouterr().out

    assert range_result.action == "target-tasks"
    assert range_result.task_ids == [1, 2, 3]
    assert all_result.action == "target-tasks"
    assert all_result.task_ids == [1, 2, 3]
    assert "Available tasks for targeting:" in out
    assert "Task 1: api | status=pending | files=src/api.py" in out
    assert "Task 3: docs | status=pending | files=docs/readme.md" in out


def test_hitl_review_target_tasks_default_note_is_gate_specific():
    cfg = _hitl_config(
        enabled=True,
        before_review=True,
        after_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="api", description="api task", files=["src/api.py"]),
        TaskItem(id=2, title="docs", description="docs task", files=["docs/readme.md"]),
    ]

    with patch("builtins.input", side_effect=["target", "2", ""]):
        before_result = flow._maybe_ask_human(
            "review", "About to review", gate="before_review"
        )

    with patch("builtins.input", side_effect=["target", "2", ""]):
        after_result = flow._maybe_ask_human(
            "review", "Automated review completed", gate="after_review"
        )

    assert before_result.action == "target-tasks"
    assert (
        before_result.instructions
        == "Human selected tasks for targeted revision before automated review."
    )
    assert after_result.action == "target-tasks"
    assert (
        after_result.instructions
        == "Human selected tasks for targeted revision after automated review."
    )


def test_hitl_review_target_tasks_supports_file_selectors_and_mixed_inputs():
    cfg = _hitl_config(
        enabled=True,
        after_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="api", description="api task", files=["src/api.py"]),
        TaskItem(
            id=2,
            title="shared docs",
            description="shared docs task",
            files=["docs/readme.md", "src/api.py"],
        ),
        TaskItem(
            id=3, title="tests", description="tests task", files=["tests/test_api.py"]
        ),
    ]
    flow.state.review_task_hints = [
        {
            "task_ids": [3],
            "files": ["tests/test_api.py"],
            "summary": "Retest the API changes",
        }
    ]

    with patch(
        "builtins.input",
        side_effect=[
            "target",
            "file:docs/readme.md, hinted, 1",
            "Use file and hinted selectors",
        ],
    ):
        result = flow._maybe_ask_human(
            "review", "Automated review completed", gate="after_review"
        )

    assert result.action == "target-tasks"
    assert result.task_ids == [2, 3, 1]
    assert flow.state.human_feedback_log[-1].task_ids == [2, 3, 1]


def test_hitl_review_target_tasks_supports_hinted_selector():
    cfg = _hitl_config(
        enabled=True,
        after_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="api", description="api task", files=["src/api.py"]),
        TaskItem(id=2, title="docs", description="docs task", files=["docs/readme.md"]),
    ]
    flow.state.review_task_hints = [
        {
            "task_ids": [2],
            "files": ["docs/readme.md"],
            "summary": "Docs task needs revision",
        }
    ]

    with patch(
        "builtins.input",
        side_effect=["target", "hinted", "Use suggested mapping"],
    ):
        result = flow._maybe_ask_human(
            "review", "Automated review completed", gate="after_review"
        )

    assert result.action == "target-tasks"
    assert result.task_ids == [2]
    assert result.instructions == "Use suggested mapping"
    assert flow.state.human_feedback_log[-1].task_ids == [2]


def test_hitl_review_target_tasks_invalid_file_selector_fails_closed():
    cfg = _hitl_config(
        enabled=True,
        after_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow.state.tasks = [
        TaskItem(id=1, title="api", description="api task", files=["src/api.py"])
    ]

    with patch("builtins.input", side_effect=["target", "file:docs/missing.md"]):
        result = flow._maybe_ask_human(
            "review", "Automated review completed", gate="after_review"
        )

    assert result.proceed is False
    assert result.action == "abort"
    assert result.task_ids is None
    assert flow.state.human_feedback_log[-1].action == "abort"


def test_review_after_review_message_includes_task_catalog_when_structured_tasks_exist(
    capsys,
):
    cfg = _hitl_config(
        enabled=True,
        after_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        tasks=[
            TaskItem(
                id=1,
                title="api",
                description="api task",
                files=["src/api.py"],
                status="done",
            ),
            TaskItem(
                id=2,
                title="docs",
                description="docs task",
                files=["docs/readme.md"],
                status="done",
            ),
        ],
        review_task_hints=[
            {
                "task_ids": [2],
                "files": ["docs/readme.md"],
                "summary": "Docs task needs revision",
            }
        ],
    )

    class RecordingWorker:
        def run(self, **kwargs):
            return CoderResult(
                summary='{"status": "revise", "issues": ["Docs task needs revision"], "summary": "docs need another pass", "task_hints": [{"task_ids": [2], "files": ["docs/readme.md"], "summary": "Docs task needs revision"}]}',
                raw_output="",
            )

    flow._workers["review"] = RecordingWorker()  # type: ignore

    with patch("builtins.input", return_value="y"):
        result = flow.review("work summary")

    out = capsys.readouterr().out

    assert result == "revise"
    assert "Suggested review targets:" in out
    assert "tasks=2 | files=docs/readme.md: Docs task needs revision" in out
    assert "Current task graph:" in out
    assert "Task 1: api | status=done | files=src/api.py" in out
    assert "Task 2: docs | status=done | files=docs/readme.md" in out


def test_plan_human_instructions_are_injected_into_prompt():
    cfg = _hitl_config(
        enabled=True,
        before_plan=True,
        capture_instructions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    class RecordingWorker:
        def __init__(self):
            self.prompts: list[str] = []

        def run(self, **kwargs):
            self.prompts.append(kwargs["task"])
            return CoderResult(
                summary='{"spec": "Plan it", "tasks": [{"id": 1, "description": "Do it"}]}',
                raw_output="",
            )

    worker = RecordingWorker()
    flow._workers["plan"] = worker  # type: ignore

    with patch("builtins.input", side_effect=["y", "Keep task slices small"]):
        result = flow.plan()

    assert "1. Do it" in result
    assert "Human approval instructions:" in worker.prompts[0]
    assert "Keep task slices small" in worker.prompts[0]
    assert flow.state.human_feedback_log[-1].stage == "plan"
    assert flow.state.human_feedback_log[-1].instructions == "Keep task slices small"


def test_plan_human_abort_sets_terminal_state():
    cfg = _hitl_config(enabled=True, before_plan=True)
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", return_value="n"):
        result = flow.plan()

    assert result == "aborted-by-human"
    assert flow.state.status == "aborted_by_human"
    assert flow.state.aborted_stage == "plan"
    assert flow.state.aborted_gate == "before_plan"
    assert (
        flow.state.aborted_gate_message
        == "About to run planning stage (plan). This is read-only but may inspect a broad slice of the repository."
    )
    assert flow.state.last_stage == "plan"
    assert flow.state.errors == ["Aborted by human before plan"]
    assert flow.state.human_feedback_log[-1].approved is False


def test_do_work_human_instructions_are_injected_into_prompt():
    cfg = _hitl_config(
        enabled=True,
        before_do_work=True,
        capture_instructions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    class RecordingWorker:
        def __init__(self):
            self.prompts: list[str] = []

        def run(self, **kwargs):
            self.prompts.append(kwargs["task"])
            return CoderResult(summary="ok", raw_output="ok")

    worker = RecordingWorker()
    flow._workers["do_work"] = worker  # type: ignore

    with patch("builtins.input", side_effect=["y", "Focus on tests first"]):
        result = flow.do_work("plan output")

    assert result == "ok"
    assert "Human approval instructions:" in worker.prompts[0]
    assert "Focus on tests first" in worker.prompts[0]


def test_do_work_human_skip_to_review_bypasses_worker():
    cfg = _hitl_config(
        enabled=True,
        before_do_work=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    class RecordingWorker:
        def __init__(self):
            self.calls = 0

        def run(self, **kwargs):
            self.calls += 1
            return CoderResult(summary="unexpected", raw_output="unexpected")

    worker = RecordingWorker()
    flow._workers["do_work"] = worker  # type: ignore

    with patch("builtins.input", return_value="review"):
        result = flow.do_work("plan output")

    assert "Human skipped do_work before edit stage" in result
    assert worker.calls == 0
    assert flow.state.last_stage == "do_work"
    assert flow.state.status == "running"


def test_do_work_human_replan_reruns_plan_before_edit_stage():
    cfg = _hitl_config(
        enabled=True,
        before_do_work=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    class RecordingPlanWorker:
        def __init__(self):
            self.prompts: list[str] = []

        def run(self, **kwargs):
            self.prompts.append(kwargs["task"])
            return CoderResult(
                summary=(
                    '{"spec": "Replanned spec", "tasks": [{"id": 1, "title": "New slice", '
                    '"description": "Implement the new slice", '
                    '"acceptance_criteria": ["Add the behavior"], '
                    '"verification": ["Run targeted tests"], '
                    '"dependencies": [], "files": ["src/math.py"], '
                    '"estimated_scope": "small"}]}'
                ),
                raw_output="",
            )

    class RecordingDoWorkWorker:
        def __init__(self):
            self.prompts: list[str] = []

        def run(self, **kwargs):
            self.prompts.append(kwargs["task"])
            return CoderResult(
                summary="implemented",
                raw_output="implemented",
                changed_files=["src/math.py"],
            )

    plan_worker = RecordingPlanWorker()
    do_work_worker = RecordingDoWorkWorker()
    flow._workers["plan"] = plan_worker  # type: ignore
    flow._workers["do_work"] = do_work_worker  # type: ignore

    with patch("builtins.input", side_effect=["replan", "Split docs from code", "y"]):
        result = flow.do_work("Old plan output")

    assert "Task 1 complete: implemented" in result
    assert "Current rendered plan markdown:" in plan_worker.prompts[0]
    assert "Old plan output" in plan_worker.prompts[0]
    assert "Replanning context:" in plan_worker.prompts[0]
    assert "Split docs from code" in plan_worker.prompts[0]
    assert "Plan / spec context:" in do_work_worker.prompts[0]
    assert "1. New slice" in do_work_worker.prompts[0]
    assert flow.state.history[-1].kind == "task_complete"
    assert any(entry.kind == "human_replanning" for entry in flow.state.history)


def test_do_work_human_target_tasks_runs_only_selected_subset(tmp_path):
    cfg = _hitl_config(
        enabled=True,
        before_do_work=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo=str(tmp_path),
        tasks=[
            TaskItem(
                id=1,
                title="api",
                description="api task",
                files=["src/api.py"],
            ),
            TaskItem(
                id=2,
                title="docs",
                description="docs task",
                files=["docs/readme.md"],
                dependencies=[1],
            ),
            TaskItem(
                id=3,
                title="qa",
                description="qa task",
                files=["tests/test_api.py"],
            ),
        ],
    )

    class RecordingWorker:
        def __init__(self):
            self.prompts: list[str] = []

        def run(self, **kwargs):
            self.prompts.append(kwargs["task"])
            return CoderResult(summary="implemented", raw_output="implemented")

    worker = RecordingWorker()
    flow._workers["do_work"] = worker  # type: ignore

    with patch(
        "builtins.input",
        side_effect=["target", "2", "Only run docs slice first"],
    ):
        result = flow.do_work("plan output")

    assert "Focused execution completed the targeted task set" in result
    assert "Pending tasks remaining: [3]" in result
    assert [entry.task_id for entry in flow.state.task_executions] == [1, 2]
    assert flow.state.tasks[0].status == "done"
    assert flow.state.tasks[1].status == "done"
    assert flow.state.tasks[2].status == "pending"
    assert any(
        entry.kind == "execution_targeting" and entry.task_ids == [1, 2]
        for entry in flow.state.history
    )
    assert "Task:\n- Id: 1" in worker.prompts[0]
    assert "Task:\n- Id: 2" in worker.prompts[1]
    assert all("Task:\n- Id: 3" not in prompt for prompt in worker.prompts)


def test_review_human_instructions_are_injected_into_prompt():
    cfg = _hitl_config(
        enabled=True,
        before_review=True,
        capture_instructions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    class RecordingWorker:
        def __init__(self):
            self.prompts: list[str] = []

        def run(self, **kwargs):
            self.prompts.append(kwargs["task"])
            return CoderResult(
                summary='{"status": "pass", "issues": [], "summary": "ok", "task_hints": []}',
                raw_output="",
            )

    worker = RecordingWorker()
    flow._workers["review"] = worker  # type: ignore

    with patch("builtins.input", side_effect=["y", "Bias toward regression risk"]):
        result = flow.review("work summary")

    assert result == "pass"
    assert "Human approval instructions:" in worker.prompts[0]
    assert "Bias toward regression risk" in worker.prompts[0]
    assert flow.state.human_feedback_log[-1].stage == "review"
    assert (
        flow.state.human_feedback_log[-1].instructions == "Bias toward regression risk"
    )


def test_review_human_force_pass_bypasses_worker():
    cfg = _hitl_config(
        enabled=True,
        before_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        issues=["old issue"],
        review_status="revise",
    )

    class RecordingWorker:
        def __init__(self):
            self.calls = 0

        def run(self, **kwargs):
            self.calls += 1
            return CoderResult(summary="unexpected", raw_output="unexpected")

    worker = RecordingWorker()
    flow._workers["review"] = worker  # type: ignore

    with patch("builtins.input", return_value="pass"):
        result = flow.review("work summary")

    assert result == "pass"
    assert worker.calls == 0
    assert flow.state.review_status == "pass"
    assert flow.state.issues == []
    assert flow.state.human_feedback_log[-1].action == "force-pass"


def test_review_human_force_revise_bypasses_worker_and_sets_issue():
    cfg = _hitl_config(
        enabled=True,
        before_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        review_status="pending",
    )

    class RecordingWorker:
        def __init__(self):
            self.calls = 0

        def run(self, **kwargs):
            self.calls += 1
            return CoderResult(summary="unexpected", raw_output="unexpected")

    worker = RecordingWorker()
    flow._workers["review"] = worker  # type: ignore

    with patch("builtins.input", side_effect=["revise", "Need more tests"]):
        result = flow.review("work summary")

    assert result == "revise"
    assert worker.calls == 0
    assert flow.state.review_status == "revise"
    assert flow.state.issues == ["Need more tests"]
    assert flow.state.human_feedback_log[-1].action == "force-revise"
    assert flow.state.human_feedback_log[-1].instructions == "Need more tests"


def test_review_human_replan_bypasses_worker_and_replans_next_revision():
    cfg = _hitl_config(
        enabled=True,
        before_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        spec="Old spec",
        tasks=[
            TaskItem(
                id=1,
                title="Old slice",
                description="Implement old slice",
                acceptance_criteria=["Old behavior"],
                verification=["Old test"],
                files=["src/old.py"],
                status="done",
            )
        ],
        review_status="pending",
    )

    class UnexpectedReviewWorker:
        def run(self, **kwargs):
            raise AssertionError("review worker should not run")

    class RecordingPlanWorker:
        def __init__(self):
            self.prompts: list[str] = []

        def run(self, **kwargs):
            self.prompts.append(kwargs["task"])
            return CoderResult(
                summary=(
                    '{"spec": "Replanned review spec", "tasks": [{"id": 1, '
                    '"title": "New slice", "description": "Implement replanned slice", '
                    '"acceptance_criteria": ["New behavior"], '
                    '"verification": ["New test"], "dependencies": [], '
                    '"files": ["src/new.py"], "estimated_scope": "small"}]}'
                ),
                raw_output="",
            )

    flow._workers["review"] = UnexpectedReviewWorker()  # type: ignore
    plan_worker = RecordingPlanWorker()
    flow._workers["plan"] = plan_worker  # type: ignore
    flow._workers["do_work"] = plan_worker  # type: ignore

    with patch("builtins.input", side_effect=["replan", "Split api from docs"]):
        result = flow.review("work summary")

    assert result == "revise"
    assert flow.state.pending_revision_replan is True
    revise_prompt = flow.revise("revise")

    assert "Target tasks for this revision:" in revise_prompt
    assert "Task 1 (New slice)" in revise_prompt
    assert flow.state.pending_revision_replan is False
    assert "Human-requested replanning guidance:" in plan_worker.prompts[0]
    assert "Split api from docs" in plan_worker.prompts[0]
    assert any(entry.kind == "human_replanning" for entry in flow.state.history)
    assert any(entry.kind == "revision_replanning" for entry in flow.state.history)


def test_review_before_review_target_tasks_bypasses_worker_and_sets_hints():
    cfg = _hitl_config(
        enabled=True,
        before_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        tasks=[
            TaskItem(
                id=1,
                title="api",
                description="api task",
                files=["src/api.py"],
                status="done",
            ),
            TaskItem(
                id=2,
                title="docs",
                description="docs task",
                files=["docs/readme.md"],
                status="done",
            ),
        ],
        review_status="pending",
    )

    class RecordingWorker:
        def __init__(self):
            self.calls = 0

        def run(self, **kwargs):
            self.calls += 1
            return CoderResult(summary="unexpected", raw_output="unexpected")

    worker = RecordingWorker()
    flow._workers["review"] = worker  # type: ignore

    with patch(
        "builtins.input",
        side_effect=["target", "2", "Only reopen docs before review"],
    ):
        result = flow.review("work summary")

    assert result == "revise"
    assert worker.calls == 0
    assert flow.state.review_status == "revise"
    assert flow.state.issues == ["Only reopen docs before review"]
    assert len(flow.state.review_task_hints) == 1
    assert flow.state.review_task_hints[0].task_ids == [2]
    assert flow.state.review_task_hints[0].files == ["docs/readme.md"]
    assert flow.state.review_task_hints[0].summary == "Only reopen docs before review"
    assert flow.state.human_feedback_log[-1].gate == "before_review"
    assert flow.state.human_feedback_log[-1].action == "target-tasks"
    assert flow.state.human_feedback_log[-1].task_ids == [2]


def test_review_after_review_force_pass_overrides_automated_revise():
    cfg = _hitl_config(
        enabled=True,
        after_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    class RecordingWorker:
        def __init__(self):
            self.calls = 0

        def run(self, **kwargs):
            self.calls += 1
            return CoderResult(
                summary='{"status": "revise", "issues": ["Need more tests"], "summary": "not ready", "task_hints": []}',
                raw_output="",
            )

    worker = RecordingWorker()
    flow._workers["review"] = worker  # type: ignore

    with patch("builtins.input", return_value="pass"):
        result = flow.review("work summary")

    assert result == "pass"
    assert worker.calls == 1
    assert flow.state.review_status == "pass"
    assert flow.state.issues == []
    assert flow.state.human_feedback_log[-1].gate == "after_review"
    assert flow.state.human_feedback_log[-1].action == "force-pass"


def test_review_after_review_force_pass_overrides_incomplete_task_auto_revise():
    cfg = _hitl_config(
        enabled=True,
        after_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        tasks=[
            TaskItem(
                id=1,
                title="api",
                description="api task",
                files=["src/api.py"],
                status="pending",
            )
        ],
    )

    class RecordingWorker:
        def __init__(self):
            self.calls = 0

        def run(self, **kwargs):
            self.calls += 1
            return CoderResult(
                summary='{"status": "pass", "issues": [], "summary": "looks good", "task_hints": []}',
                raw_output="",
            )

    worker = RecordingWorker()
    flow._workers["review"] = worker  # type: ignore

    with patch("builtins.input", return_value="pass"):
        result = flow.review("work summary")

    assert result == "pass"
    assert worker.calls == 1
    assert flow.state.review_status == "pass"
    assert flow.state.issues == []
    assert flow.state.review_task_hints == []
    assert flow.state.human_feedback_log[-1].gate == "after_review"
    assert flow.state.human_feedback_log[-1].action == "force-pass"


def test_review_after_review_force_revise_overrides_automated_pass():
    cfg = _hitl_config(
        enabled=True,
        after_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    class RecordingWorker:
        def __init__(self):
            self.calls = 0

        def run(self, **kwargs):
            self.calls += 1
            return CoderResult(
                summary='{"status": "pass", "issues": [], "summary": "looks good", "task_hints": []}',
                raw_output="",
            )

    worker = RecordingWorker()
    flow._workers["review"] = worker  # type: ignore

    with patch("builtins.input", side_effect=["revise", "Exercise edge cases"]):
        result = flow.review("work summary")

    assert result == "revise"
    assert worker.calls == 1
    assert flow.state.review_status == "revise"
    assert flow.state.issues == ["Exercise edge cases"]
    assert flow.state.human_feedback_log[-1].gate == "after_review"
    assert flow.state.human_feedback_log[-1].action == "force-revise"


def test_review_after_review_replan_overrides_automated_pass_and_replans():
    cfg = _hitl_config(
        enabled=True,
        after_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        spec="Old spec",
        tasks=[
            TaskItem(
                id=1,
                title="Old slice",
                description="Implement old slice",
                acceptance_criteria=["Old behavior"],
                verification=["Old test"],
                files=["src/old.py"],
                status="done",
            )
        ],
    )

    class RecordingReviewWorker:
        def run(self, **kwargs):
            return CoderResult(
                summary='{"status": "pass", "issues": [], "summary": "looks good", "task_hints": []}',
                raw_output="",
            )

    class RecordingPlanWorker:
        def __init__(self):
            self.prompts: list[str] = []

        def run(self, **kwargs):
            self.prompts.append(kwargs["task"])
            return CoderResult(
                summary=(
                    '{"spec": "Replanned after review spec", "tasks": [{"id": 1, '
                    '"title": "Review replan slice", '
                    '"description": "Implement review replanned slice", '
                    '"acceptance_criteria": ["Review-guided behavior"], '
                    '"verification": ["Review-guided test"], '
                    '"dependencies": [], "files": ["src/review.py"], '
                    '"estimated_scope": "small"}]}'
                ),
                raw_output="",
            )

    flow._workers["review"] = RecordingReviewWorker()  # type: ignore
    plan_worker = RecordingPlanWorker()
    flow._workers["plan"] = plan_worker  # type: ignore
    flow._workers["do_work"] = plan_worker  # type: ignore

    with patch("builtins.input", side_effect=["replan", "Resplit around docs"]):
        result = flow.review("work summary")

    assert result == "revise"
    assert flow.state.pending_revision_replan is True
    revise_prompt = flow.revise("revise")

    assert "Task 1 (Review replan slice)" in revise_prompt
    assert flow.state.pending_revision_replan is False
    assert flow.state.review_status == "pending"
    assert "Human-requested replanning guidance:" in plan_worker.prompts[0]
    assert "Resplit around docs" in plan_worker.prompts[0]
    assert any(entry.kind == "human_replanning" for entry in flow.state.history)
    assert any(entry.kind == "revision_replanning" for entry in flow.state.history)


def test_review_after_review_rerun_review_repeats_automated_review_with_guidance():
    cfg = _hitl_config(
        enabled=True,
        after_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    class RecordingWorker:
        def __init__(self):
            self.prompts: list[str] = []
            self.calls = 0

        def run(self, **kwargs):
            self.calls += 1
            self.prompts.append(kwargs["task"])
            if self.calls == 1:
                return CoderResult(
                    summary='{"status": "revise", "issues": ["Missed migration path"], "summary": "not ready", "task_hints": []}',
                    raw_output="",
                )
            return CoderResult(
                summary='{"status": "pass", "issues": [], "summary": "looks good now", "task_hints": []}',
                raw_output="",
            )

    worker = RecordingWorker()
    flow._workers["review"] = worker  # type: ignore

    with patch("builtins.input", side_effect=["rerun", "Focus on migrations", "y"]):
        result = flow.review("work summary")

    assert result == "pass"
    assert worker.calls == 2
    assert "Human rerun instructions:" not in worker.prompts[0]
    assert "Human rerun instructions:" in worker.prompts[1]
    assert "Focus on migrations" in worker.prompts[1]
    assert flow.state.review_status == "pass"
    assert any(
        entry.summary == "Human requested automated review rerun."
        for entry in flow.state.history
    )
    assert any(
        entry.action == "rerun-review" and entry.gate == "after_review"
        for entry in flow.state.human_feedback_log
    )


def test_review_after_review_target_tasks_overrides_automated_pass_and_targets_revision():
    cfg = _hitl_config(
        enabled=True,
        after_review=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        tasks=[
            TaskItem(
                id=1,
                title="api",
                description="api task",
                files=["src/api.py"],
                status="done",
            ),
            TaskItem(
                id=2,
                title="docs",
                description="docs task",
                files=["docs/readme.md"],
                status="done",
            ),
        ],
    )

    class RecordingWorker:
        def run(self, **kwargs):
            return CoderResult(
                summary='{"status": "pass", "issues": [], "summary": "looks good", "task_hints": []}',
                raw_output="",
            )

    flow._workers["review"] = RecordingWorker()  # type: ignore

    with patch(
        "builtins.input",
        side_effect=["target", "2", "Only reopen docs"],
    ):
        result = flow.review("work summary")

    assert result == "revise"
    assert flow.state.review_status == "revise"
    assert flow.state.review_task_hints[0].task_ids == [2]
    assert flow.state.review_task_hints[0].files == ["docs/readme.md"]
    assert flow.state.review_task_hints[0].summary == "Only reopen docs"
    assert flow.state.human_feedback_log[-1].action == "target-tasks"
    assert flow.state.human_feedback_log[-1].task_ids == [2]

    revise_prompt = flow.revise("revise")

    assert "Task 2 (docs)" in revise_prompt
    assert "Task 1 (api)" not in revise_prompt
    assert flow.state.tasks[0].status == "done"
    assert flow.state.tasks[1].status == "needs_revision"


def test_review_after_review_approve_can_add_revision_guidance():
    cfg = _hitl_config(
        enabled=True,
        after_review=True,
        capture_instructions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    class RecordingWorker:
        def run(self, **kwargs):
            return CoderResult(
                summary='{"status": "revise", "issues": ["Need more tests"], "summary": "not ready", "task_hints": []}',
                raw_output="",
            )

    flow._workers["review"] = RecordingWorker()  # type: ignore

    with patch("builtins.input", side_effect=["y", "Focus on API edge cases"]):
        result = flow.review("work summary")

    assert result == "revise"
    assert flow.state.issues == [
        "Need more tests",
        "Human review note: Focus on API edge cases",
    ]
    assert flow.state.human_feedback_log[-1].gate == "after_review"
    assert flow.state.human_feedback_log[-1].instructions == "Focus on API edge cases"


def test_review_after_review_abort_sets_terminal_state():
    cfg = _hitl_config(
        enabled=True,
        before_review=True,
        after_review=True,
        capture_instructions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    class RecordingWorker:
        def run(self, **kwargs):
            return CoderResult(
                summary='{"status": "pass", "issues": [], "summary": "ok", "task_hints": []}',
                raw_output="",
            )

    flow._workers["review"] = RecordingWorker()  # type: ignore

    with patch("builtins.input", side_effect=["y", "Focus on migrations", "n"]):
        result = flow.review("work summary")

    assert result == "aborted"
    assert flow.state.status == "aborted_by_human"
    assert flow.state.aborted_stage == "review"
    assert flow.state.aborted_gate == "after_review"
    assert flow.state.aborted_stage_input == "work summary"
    assert flow.state.aborted_gate_message is not None
    assert flow.state.aborted_gate_message.startswith("Automated review completed.")
    assert flow.state.aborted_before_review_instructions == "Focus on migrations"
    assert "Aborted by human at after_review" in flow.state.errors
    assert flow.state.human_feedback_log[-1].gate == "after_review"


def test_do_work_human_abort_sets_terminal_state():
    cfg = _hitl_config(enabled=True, before_do_work=True)
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", return_value="n"):
        result = flow.do_work("plan output")

    assert result == "aborted-by-human"
    assert flow.state.status == "aborted_by_human"
    assert flow.state.aborted_stage == "do_work"
    assert flow.state.aborted_gate == "before_do_work"
    assert (
        flow.state.aborted_gate_message
        == "About to run the expensive edit stage (do_work). This will let the headless coder modify files."
    )
    assert flow.state.aborted_stage_input == "plan output"
    assert flow.state.last_stage == "do_work"
    assert flow.state.errors == ["Aborted by human before do_work"]
    assert flow.state.human_feedback_log[-1].approved is False


def test_review_human_abort_sets_terminal_state():
    cfg = _hitl_config(enabled=True, before_review=True)
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", return_value="n"):
        result = flow.review("work summary")

    assert result == "aborted"
    assert flow.state.status == "aborted_by_human"
    assert flow.state.aborted_stage == "review"
    assert flow.state.aborted_gate == "before_review"
    assert (
        flow.state.aborted_gate_message
        == "About to run read-only review stage (review). This will inspect current changes and may trigger another revision loop."
    )
    assert flow.state.aborted_stage_input == "work summary"
    assert flow.state.last_stage == "review"
    assert flow.state.errors == ["Aborted by human before review"]
    assert flow.state.human_feedback_log[-1].approved is False


def test_finalize_human_abort_sets_terminal_state():
    cfg = _hitl_config(enabled=True, before_finalize=True)
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input", return_value="n"):
        result = flow.finalize("pass")

    assert result == "aborted-by-human"
    assert flow.state.status == "aborted_by_human"
    assert flow.state.aborted_stage == "finalize"
    assert flow.state.aborted_gate == "before_finalize"
    assert (
        flow.state.aborted_gate_message
        == "About to finalize and write documentation/ADR. This is the last step."
    )
    assert flow.state.aborted_stage_input == "pass"
    assert flow.state.last_stage == "finalize"
    assert flow.state.errors == ["Aborted by human before finalize"]
    assert flow.state.human_feedback_log[-1].approved is False


def test_finalize_human_skip_finalize_completes_without_worker():
    cfg = _hitl_config(
        enabled=True,
        before_finalize=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    class RecordingWorker:
        def __init__(self):
            self.calls = 0

        def run(self, **kwargs):
            self.calls += 1
            return CoderResult(summary="unexpected", raw_output="unexpected")

    worker = RecordingWorker()
    flow._workers["finalize"] = worker  # type: ignore

    with patch("builtins.input", return_value="skip"):
        result = flow.finalize("pass")

    assert result == "Finalize skipped by human after review pass."
    assert worker.calls == 0
    assert flow.state.status == "completed"
    assert flow.state.final_artifact == "Finalize skipped by human after review pass."
    assert flow.state.human_feedback_log[-1].action == "skip-finalize"


def test_finalize_human_force_revise_reopens_work_without_worker():
    cfg = _hitl_config(
        enabled=True,
        before_finalize=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        tasks=[
            TaskItem(
                id=1,
                title="API",
                description="Polish the API",
                files=["src/api.py"],
                status="done",
            )
        ],
    )

    class RecordingWorker:
        def __init__(self):
            self.calls = 0

        def run(self, **kwargs):
            self.calls += 1
            return CoderResult(summary="unexpected", raw_output="unexpected")

    worker = RecordingWorker()
    flow._workers["finalize"] = worker  # type: ignore

    with patch("builtins.input", side_effect=["revise", "Need an extra API pass"]):
        result = flow.finalize("pass")

    assert result == "revise"
    assert worker.calls == 0
    assert flow.state.status == "running"
    assert flow.state.last_stage == "finalize"
    assert flow.state.review_status == "revise"
    assert flow.state.issues == ["Need an extra API pass"]
    assert flow.state.review_task_hints == []
    assert flow.state.human_feedback_log[-1].action == "force-revise"
    assert flow.state.human_feedback_log[-1].instructions == "Need an extra API pass"


def test_finalize_human_replan_sets_pending_revision_replan_without_worker():
    cfg = _hitl_config(
        enabled=True,
        before_finalize=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        tasks=[
            TaskItem(
                id=1,
                title="API",
                description="Polish the API",
                files=["src/api.py"],
                status="done",
            )
        ],
    )

    class RecordingWorker:
        def __init__(self):
            self.calls = 0

        def run(self, **kwargs):
            self.calls += 1
            return CoderResult(summary="unexpected", raw_output="unexpected")

    worker = RecordingWorker()
    flow._workers["finalize"] = worker  # type: ignore

    with patch("builtins.input", side_effect=["replan", "Split docs from API cleanup"]):
        result = flow.finalize("pass")

    assert result == "revise"
    assert worker.calls == 0
    assert flow.state.status == "running"
    assert flow.state.last_stage == "finalize"
    assert flow.state.review_status == "revise"
    assert flow.state.pending_revision_replan is True
    assert flow.state.pending_revision_replan_reason == "Split docs from API cleanup"
    assert flow.state.issues == ["Split docs from API cleanup"]
    assert flow.state.review_task_hints == []
    assert flow.state.human_feedback_log[-1].action == "replan"
    assert (
        flow.state.human_feedback_log[-1].instructions == "Split docs from API cleanup"
    )


def test_finalize_human_rerun_review_reopens_revise_without_finalize_worker(
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = _hitl_config(
        enabled=True,
        before_finalize=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        latest_work_summary="work summary",
    )

    class RecordingWorker:
        def __init__(self):
            self.calls = 0

        def run(self, **kwargs):
            self.calls += 1
            return CoderResult(summary="unexpected", raw_output="unexpected")

    worker = RecordingWorker()
    flow._workers["finalize"] = worker  # type: ignore

    def fake_run_automated_review_once(**kwargs):
        assert kwargs["work_summary"] == "work summary"
        assert kwargs["review_rerun_guidance"] == "Focus on API edge cases"
        decision = ReviewDecision(
            status="revise",
            issues=["Missing API edge-case coverage"],
            summary="API edge cases still need work.",
        )
        flow._record_review_decision_state(decision)
        return decision

    monkeypatch.setattr(
        flow, "_run_automated_review_once", fake_run_automated_review_once
    )

    with patch("builtins.input", side_effect=["rerun", "Focus on API edge cases"]):
        result = cast(Any, flow).finalize("pass")

    assert result == "revise"
    assert worker.calls == 0
    assert flow.state.status == "running"
    assert flow.state.review_status == "revise"
    assert flow.state.issues == ["Missing API edge-case coverage"]
    assert flow.state.human_feedback_log[-1].action == "rerun-review"
    assert flow.state.human_feedback_log[-1].instructions == "Focus on API edge cases"


def test_finalize_human_rerun_review_can_return_to_finalize_prompt(
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = _hitl_config(
        enabled=True,
        before_finalize=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        latest_work_summary="work summary",
    )

    class RecordingWorker:
        def __init__(self):
            self.calls = 0

        def run(self, **kwargs):
            self.calls += 1
            return CoderResult(summary="unexpected", raw_output="unexpected")

    worker = RecordingWorker()
    flow._workers["finalize"] = worker  # type: ignore

    def fake_run_automated_review_once(**kwargs):
        assert kwargs["work_summary"] == "work summary"
        assert kwargs["review_rerun_guidance"] == "Focus on API edge cases"
        decision = ReviewDecision(
            status="pass",
            issues=[],
            summary="Review is clean.",
        )
        flow._record_review_decision_state(decision)
        return decision

    monkeypatch.setattr(
        flow, "_run_automated_review_once", fake_run_automated_review_once
    )

    with patch(
        "builtins.input",
        side_effect=["rerun", "Focus on API edge cases", "skip"],
    ):
        result = cast(Any, flow).finalize("pass")

    assert result == "Finalize skipped by human after review pass."
    assert worker.calls == 0
    assert flow.state.status == "completed"
    assert [entry.action for entry in flow.state.human_feedback_log[-2:]] == [
        "rerun-review",
        "skip-finalize",
    ]


def test_finalize_human_target_tasks_reopens_selected_tasks_without_worker():
    cfg = _hitl_config(
        enabled=True,
        before_finalize=True,
        advanced_actions=True,
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        tasks=[
            TaskItem(
                id=1,
                title="API",
                description="Polish the API",
                files=["src/api.py"],
                status="done",
            ),
            TaskItem(
                id=2,
                title="Docs",
                description="Update docs",
                files=["docs/readme.md"],
                status="done",
            ),
        ],
    )

    class RecordingWorker:
        def __init__(self):
            self.calls = 0

        def run(self, **kwargs):
            self.calls += 1
            return CoderResult(summary="unexpected", raw_output="unexpected")

    worker = RecordingWorker()
    flow._workers["finalize"] = worker  # type: ignore

    with patch(
        "builtins.input",
        side_effect=[
            "target",
            "2,file:docs/readme.md",
            "Only reopen docs before finalize",
        ],
    ):
        result = flow.finalize("pass")

    assert result == "revise"
    assert worker.calls == 0
    assert flow.state.status == "running"
    assert flow.state.last_stage == "finalize"
    assert flow.state.review_status == "revise"
    assert flow.state.issues == ["Only reopen docs before finalize"]
    assert len(flow.state.review_task_hints) == 1
    assert flow.state.review_task_hints[0].task_ids == [2]
    assert flow.state.review_task_hints[0].files == ["docs/readme.md"]
    assert flow.state.review_task_hints[0].summary == "Only reopen docs before finalize"
    assert flow.state.human_feedback_log[-1].action == "target-tasks"
    assert flow.state.human_feedback_log[-1].task_ids == [2]


# --- Conditional HITL mode (mode: conditional) integration -------------------


def _conditional_do_work_config(before_do_work: bool, min_attempts: int = 2):
    return _hitl_config(
        enabled=True,
        before_do_work=before_do_work,
        mode="conditional",
        conditional={
            "triggers": {
                "repeated_task_failure": {
                    "enabled": True,
                    "min_attempts": min_attempts,
                }
            }
        },
    )


def test_conditional_trigger_prompts_even_when_legacy_boolean_off():
    # Legacy before_do_work is False, but the enabled trigger meets threshold,
    # so conditional mode still prompts.
    cfg = _conditional_do_work_config(before_do_work=False)
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        tasks=[TaskItem(id=1, description="x", status="needs_revision")],
        task_executions=[
            TaskExecutionEntry(
                task_id=1, attempt=1, worker="grok", success=False, summary=""
            )
        ],
    )

    with patch("builtins.input", return_value="y") as mock_input:
        result = flow._maybe_ask_human("do_work", "About to do expensive work")

    assert result.proceed is True
    mock_input.assert_called_once()
    entry = flow.state.human_feedback_log[-1]
    assert entry.trigger_reason is not None
    assert entry.trigger_reason.kind == "repeated_task_failure"
    assert isinstance(entry.trigger_reason.detail, RepeatedTaskFailureDetail)
    assert entry.trigger_reason.detail.task_id == 1
    assert entry.trigger_reason.detail.attempts == 1


def test_conditional_prompt_message_shows_trigger_reason(capsys):
    cfg = _conditional_do_work_config(before_do_work=False)
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        tasks=[TaskItem(id=1, description="x", status="needs_revision")],
        task_executions=[
            TaskExecutionEntry(
                task_id=1, attempt=1, worker="grok", success=False, summary=""
            )
        ],
    )

    with patch("builtins.input", return_value="y"):
        flow._maybe_ask_human("do_work", "About to do expensive work")

    out = capsys.readouterr().out
    assert "Trigger: repeated_task_failure (task 1, 1 prior failure)" in out


def test_conditional_trigger_not_met_ignores_legacy_boolean():
    # Legacy before_do_work is True (would prompt in static mode), but under
    # conditional mode with no trigger met, the gate stays silent.
    cfg = _conditional_do_work_config(before_do_work=True)
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="test",
        target_repo="/tmp/fake",
        tasks=[TaskItem(id=1, description="x")],
        task_executions=[],
    )

    with patch("builtins.input") as mock_input:
        result = flow._maybe_ask_human("do_work", "About to do expensive work")

    assert result.proceed is True
    assert result.response == "auto-gate-disabled"
    mock_input.assert_not_called()


def test_conditional_silent_gate_has_no_phase0_trigger():
    # before_finalize has no Phase 0 trigger; under conditional mode it goes
    # silent regardless of its legacy boolean.
    cfg = _hitl_config(
        enabled=True,
        before_finalize=True,
        mode="conditional",
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="test", target_repo="/tmp/fake")  # type: ignore[attr-defined]

    with patch("builtins.input") as mock_input:
        result = flow._maybe_ask_human("finalize", "About to finalize")

    assert result.proceed is True
    assert result.response == "auto-gate-disabled"
    mock_input.assert_not_called()
