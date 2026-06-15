from __future__ import annotations

import json
from typing import Any, cast

import pytest

from crewai_headless_flow.config import FlowConfig
from crewai_headless_flow.flow import CrewAIHeadlessFlow
from crewai_headless_flow.plan_contract import PlanOutput, PlanTask
from crewai_headless_flow.state import FlowState
from crewai_headless_flow.workers.base import CoderResult


pytestmark = pytest.mark.offline


class RecordingPlanWorker:
    def __init__(self, result: CoderResult | None = None):
        self.calls: list[dict[str, Any]] = []
        self.result = result or CoderResult(
            summary=json.dumps(
                {
                    "spec": "Implement structured planning output.",
                    "tasks": [
                        {
                            "id": 1,
                            "title": "Add plan contract",
                            "description": "Create typed plan models.",
                            "acceptance_criteria": ["PlanOutput exists"],
                            "verification": ["uv run pytest -m offline"],
                            "dependencies": [],
                            "files": ["src/crewai_headless_flow/plan_contract.py"],
                            "estimated_scope": "S",
                        }
                    ],
                }
            ),
            raw_output="",
            exit_code=0,
        )

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _call_plan(flow: CrewAIHeadlessFlow) -> str:
    return cast(Any, flow).plan()


def test_plan_uses_configured_worker_and_populates_structured_state():
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
            "review": "code-review-and-quality",
            "finalize": "documentation-and-adrs",
        },
        workers={
            "plan": {"worker": "claude", "model": "sonnet", "timeout": 450},
            "do_work": {"worker": "grok", "model": "grok-3-latest"},
            "review": {"worker": "codex", "sandbox": "read-only"},
            "finalize": {"worker": "codex"},
        },
        defaults={"worker": "codex", "timeout": 300},
        human_feedback={"enabled": False},
    )
    printed: list[str] = []

    def fake_print_mapping():
        printed.append("called")

    cfg.print_mapping = fake_print_mapping  # type: ignore[method-assign]

    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="plan it", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    worker = RecordingPlanWorker()
    flow._workers["plan"] = worker  # type: ignore

    rendered = _call_plan(flow)

    assert printed == ["called"]
    assert worker.calls[0]["mode"] == "inspect"
    assert worker.calls[0]["model"] == "sonnet"
    assert worker.calls[0]["timeout"] == 450
    assert worker.calls[0]["schema"] == PlanOutput.model_json_schema()
    assert flow.state.spec == "Implement structured planning output."
    assert len(flow.state.tasks) == 1
    assert flow.state.tasks[0].title == "Add plan contract"
    assert flow.state.tasks[0].verification == ["uv run pytest -m offline"]
    assert flow.state.resolved_stages[0].worker == "claude"
    assert flow.state.resolved_stages[0].model == "sonnet"
    assert flow.state.resolved_stages[0].runtime_knobs == {}
    assert flow.state.resolved_stages[0].enforced_declarations == {}
    assert flow.state.resolved_human_feedback["enabled"] is False
    assert "1. Add plan contract" in rendered


def test_plan_crew_path_is_used_when_enabled(monkeypatch):
    cfg = FlowConfig(
        skills={
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
            "review": "code-review-and-quality",
            "finalize": "documentation-and-adrs",
        },
        workers={
            "plan": {
                "worker": "codex",
                "model": "sonnet",
                "crew": {"enabled": True, "process": "sequential"},
            },
            "do_work": {"worker": "grok", "model": "grok-3-latest"},
            "review": {"worker": "codex", "sandbox": "read-only"},
            "finalize": {"worker": "codex"},
        },
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="plan it", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    worker = RecordingPlanWorker()
    flow._workers["plan"] = worker  # type: ignore

    calls: list[dict[str, Any]] = []

    def fake_run_plan_crew(**kwargs):
        calls.append(kwargs)
        return PlanOutput(
            spec="Crew-built plan.",
            tasks=[
                PlanTask(
                    id=1,
                    title="Research",
                    description="Inspect the repo.",
                    acceptance_criteria=["Repo inspected"],
                    verification=["pytest -q"],
                    dependencies=[],
                    files=["src/example.py"],
                    estimated_scope="S",
                )
            ],
        )

    monkeypatch.setattr("crewai_headless_flow.flow.run_plan_crew", fake_run_plan_crew)

    rendered = _call_plan(flow)

    assert calls
    assert calls[0]["worker_tool"] is worker
    assert calls[0]["model"] == "sonnet"
    assert flow.state.spec == "Crew-built plan."
    assert flow.state.resolved_stages[0].runtime_knobs == {"crew": {"enabled": True}}
    assert flow.state.resolved_stages[0].enforced_declarations == {
        "crew": {"process": "sequential"}
    }
    assert flow.state.resolved_stages[0].notes == ["crew_llm_provider=ollama-local"]
    assert "1. Research" in rendered


def test_plan_fails_closed_on_empty_structured_output():
    cfg = FlowConfig(
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
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(request="plan it", target_repo="/tmp/fake")  # type: ignore[attr-defined]
    flow._workers["plan"] = RecordingPlanWorker(
        result=CoderResult(
            summary='{"spec": "", "tasks": []}', raw_output="", exit_code=0
        )
    )  # type: ignore

    with pytest.raises(RuntimeError, match="invalid structured plan"):
        _call_plan(flow)


def test_flow_worker_uses_overridden_stage_skill():
    cfg = FlowConfig(
        skills={
            "plan": "test-driven-development",
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
    )

    flow = CrewAIHeadlessFlow(config=cfg)

    assert flow._workers["plan"].skill_name == "test-driven-development"  # type: ignore[attr-defined]
