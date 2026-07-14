from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import cast

import pytest
from crewai import LLM, Process

from crewai_headless_flow.crew_defs import (
    build_agents_from_yaml,
    build_crew,
    build_tasks_from_yaml,
    crew_defs_dir,
    load_crew_yaml,
    tool_agent_map,
)
from crewai_headless_flow.plan_contract import PlanOutput
from crewai_headless_flow.plan_crew import build_plan_crew
from crewai_headless_flow.tools.coder_tool import HeadlessCoderTool
from crewai_headless_flow.workers.base import CoderResult


pytestmark = pytest.mark.offline

CREW_NAMES = (
    "plan",
    "review",
    "do_work_round",
    "do_work_decomposition",
)


class RecordingWorkerTool:
    def run(self, **kwargs):
        return CoderResult(summary="ok", raw_output="", exit_code=0)


def _write_plan_crew(
    config_dir: Path, *, researcher_role: str = "Custom Plan Researcher"
) -> None:
    crew_dir = config_dir / "crews" / "plan"
    crew_dir.mkdir(parents=True)
    (crew_dir / "agents.yaml").write_text(
        dedent(
            f"""\
            researcher:
              role: {researcher_role}
              goal: Gather grounded repository context for planning.
              backstory: Custom backstory.
            planner:
              role: Implementation Planner
              goal: Draft a concrete implementation plan from grounded evidence.
              backstory: You break requests into scoped, testable tasks.
            validator:
              role: Plan Validator
              goal: Catch missing dependencies, files, verification steps, and scope drift.
              backstory: You strengthen plans before they are executed.
            coordinator:
              role: Planning Coordinator
              goal: Produce the final structured implementation plan.
              backstory: You reconcile research, planning, and validation into one plan.
            """
        ),
        encoding="utf-8",
    )
    (crew_dir / "tasks.yaml").write_text(
        dedent(
            """\
            research_task:
              description: |
                Use the read-only inspection tool to gather planning context.

                {context}
              expected_output: A concise research summary.
              agent: researcher
            draft_task:
              description: Draft an implementation plan.
              expected_output: A draft plan.
              agent: planner
              context:
                - research_task
            validation_task:
              description: Review the draft plan.
              expected_output: Concrete plan issues or an explicit no-issues statement.
              agent: validator
              context:
                - research_task
                - draft_task
            final_task:
              description: Produce the final structured implementation plan.
              expected_output: A JSON object with spec and tasks.
              agent: coordinator
              context:
                - research_task
                - draft_task
                - validation_task
            """
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("crew_name", CREW_NAMES)
def test_crew_yaml_defs_exist_and_load(crew_name: str):
    agents_config, tasks_config = load_crew_yaml(crew_name)

    assert agents_config
    assert tasks_config
    assert (crew_defs_dir() / crew_name / "agents.yaml").is_file()
    assert (crew_defs_dir() / crew_name / "tasks.yaml").is_file()


def test_build_agents_and_tasks_from_plan_yaml_injects_context_and_pydantic():
    agents_config, tasks_config = load_crew_yaml("plan")
    llm = LLM(
        model="ollama/llama3.2",
        base_url="http://localhost:11434",
        temperature=0.2,
    )
    agents = build_agents_from_yaml(
        agents_config,
        llm=llm,
        delegation_agent_keys={"coordinator"},
        allow_delegation=True,
    )
    tasks = build_tasks_from_yaml(
        tasks_config,
        agents,
        assign_agents=True,
        description_vars={"context": "Ship the feature."},
        output_pydantic_by_task={"final_task": PlanOutput},
    )

    assert agents["coordinator"].allow_delegation is True
    assert agents["researcher"].allow_delegation is False
    assert "Ship the feature." in tasks[0].description
    assert tasks[-1].output_pydantic is PlanOutput
    crew = build_crew(agents=agents, tasks=tasks, crew_config={})
    assert crew.process == Process.sequential


def test_load_crew_yaml_missing_raises(tmp_path: Path):
    # A crews/ tree that exists but lacks this crew should not silently fall back.
    (tmp_path / "crews").mkdir()
    with pytest.raises(FileNotFoundError):
        load_crew_yaml("plan", config_dir=tmp_path)


def test_load_crew_yaml_prefers_config_dir_when_present(tmp_path: Path):
    _write_plan_crew(tmp_path, researcher_role="Override Researcher")

    agents_config, _tasks = load_crew_yaml("plan", config_dir=tmp_path)

    assert agents_config["researcher"]["role"] == "Override Researcher"


def test_load_crew_yaml_falls_back_to_default_when_config_dir_lacks_crews(
    tmp_path: Path,
):
    agents_config, _tasks = load_crew_yaml("plan", config_dir=tmp_path)

    assert agents_config["researcher"]["role"] == "Repository Researcher"


def test_build_plan_crew_uses_config_dir_agent_roles(tmp_path: Path):
    _write_plan_crew(tmp_path, researcher_role="Pack-Local Researcher")

    crew = build_plan_crew(
        planning_context="Plan it.",
        worker_tool=cast(HeadlessCoderTool, RecordingWorkerTool()),
        cwd="/tmp/repo",
        timeout=17,
        config_dir=tmp_path,
    )

    assert any(a.role == "Pack-Local Researcher" for a in crew.agents)


def test_tool_agent_map_builds_mapping():
    tools = {"a": [object()], "b": [object()]}  # type: ignore[list-item]
    mapping = tool_agent_map(
        ("a", tools["a"]),
        ("b", tools["b"]),
    )

    assert mapping == tools


def test_tool_agent_map_rejects_empty_tool_list():
    with pytest.raises(ValueError, match="empty"):
        tool_agent_map(("researcher", []))


def test_tool_agent_map_rejects_duplicate_agent_key():
    tools = [object()]  # type: ignore[list-item]
    with pytest.raises(ValueError, match="duplicate"):
        tool_agent_map(
            ("researcher", tools),
            ("researcher", tools),
        )


def test_build_agents_from_yaml_rejects_missing_tool_agent_keys():
    agents_config, _tasks = load_crew_yaml("plan")
    agents_config = {k: v for k, v in agents_config.items() if k != "researcher"}
    llm = LLM(
        model="ollama/llama3.2",
        base_url="http://localhost:11434",
        temperature=0.2,
    )

    with pytest.raises(KeyError, match="researcher"):
        build_agents_from_yaml(
            agents_config,
            llm=llm,
            tools_by_agent={"researcher": [object()]},  # type: ignore[list-item]
        )


def test_build_agents_from_yaml_rejects_empty_tool_list():
    agents_config, _tasks = load_crew_yaml("plan")
    llm = LLM(
        model="ollama/llama3.2",
        base_url="http://localhost:11434",
        temperature=0.2,
    )

    with pytest.raises(KeyError, match="researcher"):
        build_agents_from_yaml(
            agents_config,
            llm=llm,
            tools_by_agent={"researcher": [], "planner": [object()]},  # type: ignore[list-item]
        )


def test_build_tasks_from_yaml_rejects_unknown_output_pydantic_task_key():
    agents_config, tasks_config = load_crew_yaml("plan")
    llm = LLM(
        model="ollama/llama3.2",
        base_url="http://localhost:11434",
        temperature=0.2,
    )
    agents = build_agents_from_yaml(agents_config, llm=llm)

    with pytest.raises(KeyError, match="missing_task"):
        build_tasks_from_yaml(
            tasks_config,
            agents,
            assign_agents=True,
            output_pydantic_by_task={"missing_task": PlanOutput},
        )
