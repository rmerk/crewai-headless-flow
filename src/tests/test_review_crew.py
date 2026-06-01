from __future__ import annotations

from types import SimpleNamespace

import pytest

from crewai_headless_flow.review_crew import (
    HeadlessInspectTool,
    ReviewCrewDecision,
    normalize_review_crew_output,
)
from crewai_headless_flow.workers.base import CoderResult


pytestmark = pytest.mark.offline


class RecordingWorkerTool:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return CoderResult(
            summary="inspected",
            raw_output="inspect details",
            exit_code=0,
        )


def test_normalize_accepts_pydantic_crew_output_pass():
    crew_output = SimpleNamespace(
        pydantic=ReviewCrewDecision(
            status="pass",
            issues=[],
            summary="Looks good.",
        )
    )

    decision = normalize_review_crew_output(crew_output)

    assert decision.status == "pass"
    assert decision.issues == []
    assert decision.summary == "Looks good."


def test_normalize_accepts_json_dict_crew_output_revise():
    crew_output = SimpleNamespace(
        pydantic=None,
        json_dict={
            "status": "revise",
            "issues": ["Missing regression test"],
            "summary": "Needs test coverage.",
        },
    )

    decision = normalize_review_crew_output(crew_output)

    assert decision.status == "revise"
    assert decision.issues == ["Missing regression test"]
    assert decision.summary == "Needs test coverage."


def test_normalize_converts_pass_with_issues_to_revise():
    crew_output = SimpleNamespace(
        pydantic=None,
        json_dict={
            "status": "pass",
            "issues": ["A concrete issue remains"],
            "summary": "Contradictory output.",
        },
    )

    decision = normalize_review_crew_output(crew_output)

    assert decision.status == "revise"
    assert decision.issues == ["A concrete issue remains"]


def test_normalize_fails_closed_on_malformed_output():
    crew_output = SimpleNamespace(pydantic=None, json_dict=None, raw="not json")

    decision = normalize_review_crew_output(crew_output)

    assert decision.status == "revise"
    assert decision.issues == ["Review Crew output could not be parsed"]


def test_headless_inspect_tool_always_calls_inspect_mode():
    worker_tool = RecordingWorkerTool()
    tool = HeadlessInspectTool(worker_tool=worker_tool, cwd="/tmp/repo", timeout=17)

    result = tool._run(prompt="Review these changes")

    assert result == "inspected"
    assert worker_tool.calls == [
        {
            "task": "Review these changes",
            "cwd": "/tmp/repo",
            "mode": "inspect",
            "timeout": 17,
        }
    ]


def test_headless_inspect_tool_public_run_path_uses_inspect_mode():
    worker_tool = RecordingWorkerTool()
    tool = HeadlessInspectTool(worker_tool=worker_tool, cwd="/tmp/repo", timeout=17)

    result = tool.run(prompt="Review these changes")

    assert result == "inspected"
    assert worker_tool.calls[0]["mode"] == "inspect"
