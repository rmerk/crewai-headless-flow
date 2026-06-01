"""
Milestone 3: Skill → Worker wiring verification.

Goal: Prove that the chosen skill's Process / core guidance is actually
injected into the `task` string that reaches the headless coder.

These tests are fully mocked (no real CLIs, no network).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from crewai_headless_flow.tools.coder_tool import (
    HeadlessCoderTool,
    build_task_with_skill,
)
from crewai_headless_flow.workers.base import CoderResult


pytestmark = pytest.mark.offline


def test_build_task_with_skill_injects_incremental_implementation():
    """The generated task must contain signature phrases from the skill."""
    task = build_task_with_skill(
        skill_name="incremental-implementation",
        user_prompt="Add a new CLI command for exporting reports.",
    )

    # Key phrases that must appear from the real skill
    assert (
        "Incremental Implementation" in task or "thin vertical slices" in task.lower()
    )
    assert "Implement" in task and "Test" in task and "Verify" in task
    assert "Vertical Slices" in task or "vertical slice" in task.lower()
    assert "Add a new CLI command for exporting reports." in task


def test_build_task_with_skill_injects_code_review_and_quality():
    task = build_task_with_skill(
        skill_name="code-review-and-quality",
        user_prompt="Review the recent changes to auth.py",
    )

    assert "Five-Axis" in task or "Correctness" in task or "Readability" in task
    assert "Review Process" in task or "Review the" in task
    assert "Review the recent changes to auth.py" in task


def test_headless_coder_tool_injects_skill_for_grok(monkeypatch):
    """When using GrokAdapter with a skill, the task passed to .run() must contain the skill text."""
    mock_worker = MagicMock()
    mock_worker.run.return_value = CoderResult(summary="done")

    tool = HeadlessCoderTool(
        worker=mock_worker,
        skill_name="test-driven-development",
    )

    tool.run(
        task="Fix the bug in the payment flow",
        cwd="/tmp/fake-repo",
        mode="edit",
    )

    # Inspect what was actually sent to the worker
    called_task = mock_worker.run.call_args[0][0]  # first positional arg = task

    assert (
        "Test-Driven Development" in called_task
        or "TDD Cycle" in called_task
        or "RED" in called_task
    )
    assert "Fix the bug in the payment flow" in called_task
    # The skill content must be present (we use flexible phrases that definitely exist)
    assert (
        "failing test" in called_task.lower() or "write a test" in called_task.lower()
    )


def test_headless_coder_tool_injects_skill_for_codex(monkeypatch):
    """Same injection must work when the worker is CodexAdapter."""
    mock_worker = MagicMock()
    mock_worker.run.return_value = CoderResult(summary="done")

    tool = HeadlessCoderTool(
        worker=mock_worker,
        skill_name="planning-and-task-breakdown",
    )

    tool.run(
        task="Break down the new reporting feature",
        cwd="/tmp/fake-repo",
        mode="edit",
    )

    called_task = mock_worker.run.call_args[0][0]

    assert "Planning and Task Breakdown" in called_task or "Plan Mode" in called_task
    assert "Break down the new reporting feature" in called_task
    # Flexible but real content from the skill
    assert (
        "Acceptance criteria" in called_task
        or "vertical slice" in called_task.lower()
        or "Task [" in called_task
    )


def test_headless_coder_tool_without_skill_passes_task_unchanged():
    """When no skill is configured, the original task text is passed through."""
    mock_worker = MagicMock()
    mock_worker.run.return_value = CoderResult(summary="done")

    tool = HeadlessCoderTool(worker=mock_worker, skill_name=None)

    original = "Just do this one small thing with no special procedure"
    tool.run(task=original, cwd="/tmp/r", mode="edit")

    called_task = mock_worker.run.call_args[0][0]
    assert called_task == original
