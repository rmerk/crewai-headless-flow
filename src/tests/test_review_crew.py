from __future__ import annotations

from types import SimpleNamespace

import pytest
from crewai import Process

from crewai_headless_flow.review_crew import (
    HeadlessInspectTool,
    ReviewCrewDecision,
    build_review_crew,
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


def test_normalize_review_crew_output_invalid_task_hints_fail_closed():
    crew_output = SimpleNamespace(
        pydantic=None,
        json_dict={
            "status": "pass",
            "issues": [],
            "summary": "Looks good.",
            "task_hints": [{"task_ids": "oops", "summary": "Bad mapping"}],
        },
    )

    decision = normalize_review_crew_output(crew_output)

    assert decision.status == "revise"
    assert decision.task_hints == []
    assert any("invalid task_hints" in issue for issue in decision.issues)


def test_headless_inspect_tool_always_calls_inspect_mode():
    worker_tool = RecordingWorkerTool()
    tool = HeadlessInspectTool(
        worker_tool=worker_tool,
        cwd="/tmp/repo",
        timeout=17,
        model="sonnet",
    )

    result = tool._run(prompt="Review these changes")

    assert result == "inspected"
    assert worker_tool.calls == [
        {
            "task": "Review these changes",
            "cwd": "/tmp/repo",
            "mode": "inspect",
            "timeout": 17,
            "model": "sonnet",
        }
    ]


def test_headless_inspect_tool_public_run_path_uses_inspect_mode():
    worker_tool = RecordingWorkerTool()
    tool = HeadlessInspectTool(worker_tool=worker_tool, cwd="/tmp/repo", timeout=17)

    result = tool.run(prompt="Review these changes")

    assert result == "inspected"
    assert worker_tool.calls[0]["mode"] == "inspect"


def _build_review_crew(**crew_config_overrides):
    return build_review_crew(
        review_context="Review the recent changes.",
        worker_tool=RecordingWorkerTool(),
        cwd="/tmp/repo",
        timeout=17,
        crew_config=crew_config_overrides,
    )


def test_build_review_crew_defaults_to_sequential_without_delegation():
    crew = _build_review_crew()

    assert crew.process == Process.sequential
    coordinator = next(a for a in crew.agents if a.role == "Review Coordinator")
    assert coordinator.allow_delegation is False
    assert all(task.agent is not None for task in crew.tasks)


def test_build_review_crew_sequential_delegation_enabled_only_on_coordinator():
    crew = _build_review_crew(process="sequential", delegation={"enabled": True})

    assert crew.process == Process.sequential
    for agent in crew.agents:
        if agent.role == "Review Coordinator":
            assert agent.allow_delegation is True
        else:
            assert agent.allow_delegation is False


def test_build_review_crew_hierarchical_uses_manager_llm_and_unassigned_tasks():
    crew = _build_review_crew(
        process="hierarchical",
        manager={"llm": {"model": "gpt-4o"}},
    )

    assert crew.process == Process.hierarchical
    assert crew.manager_llm is not None
    assert crew.manager_llm.model == "gpt-4o"
    assert all(task.agent is None for task in crew.tasks)


def test_build_review_crew_hierarchical_manager_llm_falls_back_to_crew_llm():
    crew = _build_review_crew(
        process="hierarchical",
        llm={"model": "custom-fallback-model"},
    )

    assert crew.manager_llm.model == "custom-fallback-model"


def test_build_review_crew_hierarchical_ignores_delegation_flag():
    crew = _build_review_crew(process="hierarchical", delegation={"enabled": True})

    coordinator = next(a for a in crew.agents if a.role == "Review Coordinator")
    assert coordinator.allow_delegation is False


def test_build_review_crew_hierarchical_preserves_explicit_zero_temperature():
    crew = _build_review_crew(
        process="hierarchical",
        manager={"llm": {"model": "gpt-4o", "temperature": 0.0}},
    )

    assert crew.manager_llm.temperature == 0.0
