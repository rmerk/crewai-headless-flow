from __future__ import annotations

import pytest

from crewai_headless_flow.reporting import render_execution_report
from crewai_headless_flow.state import (
    AbortedCheckpoint,
    CrewRoundEntry,
    FlowHistoryEntry,
    FlowState,
    HumanFeedbackEntry,
    StageRuntimeSnapshot,
    TaskItem,
    TaskExecutionEntry,
)


pytestmark = pytest.mark.offline


def test_render_execution_report_includes_tasks_issues_and_history():
    state = FlowState(
        status="running",
        review_status="revise",
        revisions=1,
        max_revisions=2,
        changed_files=["src/a.py"],
        issues=["Need more tests"],
        resolved_stages=[
            StageRuntimeSnapshot(
                stage="review",
                skill="code-review-and-quality",
                worker="codex",
                timeout=300,
                extra={"sandbox": "read-only"},
                runtime_knobs={},
                enforced_declarations={"sandbox": "read-only"},
                notes=[],
                can_mutate=False,
            )
        ],
        resolved_human_feedback={
            "enabled": True,
            "before_do_work": True,
            "before_finalize": False,
            "capture_instructions": True,
        },
        human_feedback_log=[
            HumanFeedbackEntry(
                stage="do_work",
                approved=True,
                action="approve",
                response="y",
                instructions="Focus on tests first",
                revision=0,
                worker="grok",
                skill="incremental-implementation",
                can_mutate=True,
                message="About to do expensive work",
            )
        ],
        tasks=[
            TaskItem(
                id=1,
                title="Add tests",
                description="Add regression tests",
                files=["src/a.py"],
                review_notes=["Need more tests"],
                status="needs_revision",
            )
        ],
        history=[
            FlowHistoryEntry(
                kind="review_decision",
                revision=1,
                summary="Review returned revise.",
                task_ids=[1],
                files=["src/a.py"],
                details=["Need more tests"],
            )
        ],
        task_executions=[
            TaskExecutionEntry(
                task_id=1,
                attempt=1,
                revision=1,
                worker="grok",
                orchestration="crew",
                success=False,
                summary="Needs another pass.",
                error="Implementation Crew requested revision: Need more tests",
                changed_files=["src/a.py"],
                isolated_workspace=True,
                workspace="/tmp/flow-parallel-task-1/repo",
                parallel_batch_id="b1",
                crew_rounds=[
                    CrewRoundEntry(
                        round=1,
                        subtask_id=2,
                        subtask_title="Wire flow",
                        decision_status="revise",
                        decision_summary="Needs another pass.",
                        decision_issues=["Need more tests"],
                        result_summary="Round 1 summary",
                    )
                ],
            )
        ],
    )

    report = render_execution_report(state)

    assert "# Flow Execution Report" in report
    assert "## Runtime Configuration" in report
    assert "skill=code-review-and-quality | worker=codex" in report
    assert 'Enforced declarations: {"sandbox": "read-only"}' in report
    assert 'Raw extra: {"sandbox": "read-only"}' in report
    assert '"capture_instructions": true' in report
    assert "## Human Feedback" in report
    assert "r0 do_work approve worker=grok response=y" in report
    assert "Focus on tests first" in report
    assert "Task 1 [needs_revision]" in report
    assert "Need more tests" in report
    assert "## Task Executions" in report
    assert "task=1 attempt=1 r1 path=crew success=no worker=grok" in report
    assert "batch=b1 isolated=yes" in report
    assert "Round 1 [subtask 2: Wire flow]: revise | Needs another pass." in report
    assert "r1 review_decision tasks=1 files=src/a.py" in report


def test_render_execution_report_shows_nondefault_human_feedback_gate():
    state = FlowState(
        human_feedback_log=[
            HumanFeedbackEntry(
                stage="review",
                gate="after_review",
                approved=False,
                action="force-pass",
                response="pass",
                revision=1,
                worker="codex",
                skill="code-review-and-quality",
                can_mutate=False,
                message="Automated review completed.",
                task_ids=[2, 4],
            )
        ]
    )

    report = render_execution_report(state)

    assert "r1 review@after_review force-pass worker=codex response=pass" in report
    assert "Target tasks: 2, 4" in report


def test_render_execution_report_shows_aborted_checkpoint():
    state = FlowState(
        status="aborted_by_human",
        aborted_checkpoint=AbortedCheckpoint(
            stage="review",
            gate="after_review",
            message="Automated review completed.\nSuggested status: revise",
            before_review_instructions="Focus on migrations",
            stage_input="Summary line\nSecond line",
        ),
    )

    report = render_execution_report(state)

    assert "- Aborted checkpoint: review@after_review" in report
    assert "- Aborted checkpoint message:" in report
    assert "  Automated review completed." in report
    assert "  Suggested status: revise" in report
    assert "- Saved before_review instructions:" in report
    assert "  Focus on migrations" in report
    assert "- Saved resume input: 24 chars across 2 line(s)" in report
    assert "- Resume input preview:" in report
    assert "  Summary line" in report
    assert "  Second line" in report


def test_render_execution_report_shows_saved_latest_review_input_when_distinct():
    state = FlowState(
        status="aborted_by_human",
        latest_work_summary="Implemented API cleanup\nAdded regression tests",
        aborted_checkpoint=AbortedCheckpoint(
            stage="finalize",
            gate="before_finalize",
            message="About to finalize and write documentation/ADR. This is the last step.",
            stage_input="pass",
        ),
    )

    report = render_execution_report(state)

    assert "- Aborted checkpoint: finalize@before_finalize" in report
    assert "- Saved resume input: 4 chars across 1 line(s)" in report
    assert "- Saved latest review input: 46 chars across 2 line(s)" in report
    assert "- Latest review input preview:" in report
    assert "  Implemented API cleanup" in report
    assert "  Added regression tests" in report


def test_render_execution_report_shows_pending_revision_replan_reason():
    state = FlowState(
        pending_revision_replan=True,
        pending_revision_replan_reason="Human requested replanning before the next revise loop.",
    )

    report = render_execution_report(state)

    assert "- Pending revision replan: yes" in report
    assert "  Reason: Human requested replanning before the next revise loop." in report


def test_render_execution_report_shows_execution_targeting_history():
    state = FlowState(
        history=[
            FlowHistoryEntry(
                kind="execution_targeting",
                revision=0,
                summary="Human narrowed do_work to targeted tasks.",
                task_ids=[1, 2],
                files=["src/api.py", "docs/readme.md"],
                details=[
                    "Requested tasks: 2",
                    "Auto-included dependency tasks: 1",
                    "Only run docs slice first",
                ],
            )
        ]
    )

    report = render_execution_report(state)

    assert (
        "- r0 execution_targeting tasks=1,2 files=src/api.py,docs/readme.md: "
        "Human narrowed do_work to targeted tasks."
    ) in report
    assert "  - Requested tasks: 2" in report
    assert "  - Auto-included dependency tasks: 1" in report


def test_render_execution_report_shows_runtime_knobs_and_notes():
    state = FlowState(
        resolved_stages=[
            StageRuntimeSnapshot(
                stage="plan",
                skill="planning-and-task-breakdown",
                worker="codex",
                model="sonnet",
                timeout=300,
                extra={
                    "crew": {
                        "enabled": True,
                        "process": "sequential",
                        "llm": {
                            "model": "gpt-4o-mini",
                            "base_url": "https://api.openai.com/v1",
                        },
                    }
                },
                runtime_knobs={
                    "crew": {
                        "enabled": True,
                        "llm": {
                            "model": "gpt-4o-mini",
                            "base_url": "https://api.openai.com/v1",
                        },
                    }
                },
                enforced_declarations={"crew": {"process": "sequential"}},
                notes=["crew_llm_provider=external/custom"],
                can_mutate=False,
            )
        ]
    )

    report = render_execution_report(state)

    assert (
        'Runtime knobs: {"crew": {"enabled": true, "llm": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"}}}'
        in report
    )
    assert 'Enforced declarations: {"crew": {"process": "sequential"}}' in report
    assert "Notes: crew_llm_provider=external/custom" in report


def test_render_execution_report_infers_classification_from_legacy_extra_only():
    state = FlowState(
        resolved_stages=[
            StageRuntimeSnapshot(
                stage="review",
                skill="code-review-and-quality",
                worker="codex",
                timeout=300,
                extra={
                    "sandbox": "read-only",
                    "crew": {
                        "enabled": True,
                        "process": "sequential",
                        "llm": {
                            "model": "ollama/llama3.2",
                            "base_url": "http://localhost:11434",
                        },
                    },
                },
                can_mutate=False,
            )
        ]
    )

    report = render_execution_report(state)

    assert (
        'Runtime knobs: {"crew": {"enabled": true, "llm": {"base_url": "http://localhost:11434", "model": "ollama/llama3.2"}}}'
        in report
    )
    assert (
        'Enforced declarations: {"crew": {"process": "sequential"}, "sandbox": "read-only"}'
        in report
    )
    assert "Notes: crew_llm_provider=ollama-local" in report
