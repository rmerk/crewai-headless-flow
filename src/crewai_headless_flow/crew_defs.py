"""CrewBase-style YAML loading for optional stage crews.

Agent/task *text* lives under ``config/crews/<name>/{agents,tasks}.yaml``.
Python still injects runtime tools, LLMs, process/delegation overlays from
``worker.yaml``, and dynamic context into task descriptions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from crewai import Agent, Crew, LLM, Process, Task
from crewai.tools import BaseTool

from .config import DEFAULT_CONFIG_DIR


class _FormatMap(dict[str, Any]):
    """Leave unknown ``{placeholders}`` untouched during description formatting."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def crew_defs_dir(config_dir: Path | None = None) -> Path:
    """Return the ``crews/`` directory under the active config root."""
    return (config_dir or DEFAULT_CONFIG_DIR) / "crews"


def _crew_bundle_dir(crew_name: str, config_dir: Path | None) -> Path | None:
    """Return ``…/crews/<name>`` when both agents.yaml and tasks.yaml exist."""
    if config_dir is None:
        return None
    base = Path(config_dir) / "crews" / crew_name
    if (base / "agents.yaml").is_file() and (base / "tasks.yaml").is_file():
        return base
    return None


def resolve_crew_bundle_dir(
    crew_name: str, *, config_dir: Path | str | None = None
) -> Path:
    """Resolve a crew YAML bundle for ``crew_name``.

    Prefer ``<config_dir>/crews/<name>/`` when that bundle is complete. If
    ``config_dir`` has no ``crews/`` directory at all, fall back to the default
    config pack so example configs that only override ``worker.yaml`` /
    ``skills.yaml`` keep working. If ``config_dir/crews/`` exists but the named
    crew is incomplete/missing, fail closed instead of mixing packs.
    """
    resolved_config: Path | None = Path(config_dir) if config_dir else None
    preferred = _crew_bundle_dir(crew_name, resolved_config)
    if preferred is not None:
        return preferred

    if resolved_config is not None and (resolved_config / "crews").is_dir():
        base = resolved_config / "crews" / crew_name
        raise FileNotFoundError(
            f"Missing complete crew bundle under {base} "
            "(expected agents.yaml and tasks.yaml)"
        )

    default = _crew_bundle_dir(crew_name, DEFAULT_CONFIG_DIR)
    if default is not None:
        return default

    raise FileNotFoundError(
        f"Missing complete crew bundle for {crew_name!r} under "
        f"{DEFAULT_CONFIG_DIR / 'crews' / crew_name}"
    )


def load_crew_yaml(
    crew_name: str, *, config_dir: Path | str | None = None
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Load agents.yaml and tasks.yaml for a named crew definition."""
    base = resolve_crew_bundle_dir(crew_name, config_dir=config_dir)
    agents_path = base / "agents.yaml"
    tasks_path = base / "tasks.yaml"

    agents_config = _load_mapping_yaml(agents_path, label="agents")
    tasks_config = _load_mapping_yaml(tasks_path, label="tasks")
    return agents_config, tasks_config


def _load_mapping_yaml(path: Path, *, label: str) -> dict[str, dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(
            f"{path} {label} must be a mapping, got {type(loaded).__name__}"
        )
    result: dict[str, dict[str, Any]] = {}
    for key, value in loaded.items():
        if not isinstance(key, str):
            raise ValueError(f"{path} {label} keys must be strings")
        if not isinstance(value, dict):
            raise ValueError(
                f"{path} {label}[{key!r}] must be a mapping, got {type(value).__name__}"
            )
        result[key] = value
    return result


def crew_llm(crew_config: dict[str, Any]) -> LLM:
    llm_cfg = crew_config.get("llm", {}) or {}
    return LLM(
        model=llm_cfg.get("model", "ollama/llama3.2"),
        base_url=llm_cfg.get("base_url", "http://localhost:11434"),
        temperature=llm_cfg.get("temperature", 0.2),
    )


def is_hierarchical(crew_config: dict[str, Any]) -> bool:
    return crew_config.get("process", "sequential") == "hierarchical"


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def manager_llm(crew_config: dict[str, Any]) -> LLM:
    """LLM for the auto-created manager agent (hierarchical mode only).

    Falls back to the crew's own ``llm`` block for any field left unset, so a
    minimal ``manager.llm.model`` override is enough to point the manager at
    a stronger tool-calling model while reusing the crew's base_url/temperature.
    ``None`` (not just "missing key") is treated as unset so an explicit
    ``temperature: 0.0`` override is never mistaken for "fall back".
    """
    fallback_cfg = crew_config.get("llm", {}) or {}
    manager_cfg = (crew_config.get("manager", {}) or {}).get("llm", {}) or {}
    return LLM(
        model=_first_not_none(
            manager_cfg.get("model"), fallback_cfg.get("model"), "ollama/llama3.2"
        ),
        base_url=_first_not_none(
            manager_cfg.get("base_url"),
            fallback_cfg.get("base_url"),
            "http://localhost:11434",
        ),
        temperature=_first_not_none(
            manager_cfg.get("temperature"), fallback_cfg.get("temperature"), 0.2
        ),
    )


def delegation_enabled(crew_config: dict[str, Any]) -> bool:
    """Sequential-only: whether coordinator agents get allow_delegation=True.

    Hierarchical mode always routes execution through CrewAI's auto-created
    manager, so this flag is only meaningful under Process.sequential.
    """
    if is_hierarchical(crew_config):
        return False
    delegation_cfg = crew_config.get("delegation", {}) or {}
    return bool(delegation_cfg.get("enabled", False))


def build_agents_from_yaml(
    agents_config: dict[str, dict[str, Any]],
    *,
    llm: LLM,
    tools_by_agent: dict[str, list[BaseTool]] | None = None,
    delegation_agent_keys: set[str] | frozenset[str] = frozenset(),
    allow_delegation: bool = False,
) -> dict[str, Agent]:
    """Build agents from CrewBase-style agents.yaml, injecting tools/LLM."""
    tools_by_agent = tools_by_agent or {}
    agents: dict[str, Agent] = {}
    for name, raw in agents_config.items():
        config = dict(raw)
        config.pop("tools", None)
        agents[name] = Agent(
            config=config,
            tools=list(tools_by_agent.get(name, [])),
            llm=llm,
            verbose=False,
            allow_delegation=bool(allow_delegation and name in delegation_agent_keys),
        )
    return agents


def build_tasks_from_yaml(
    tasks_config: dict[str, dict[str, Any]],
    agents: dict[str, Agent],
    *,
    assign_agents: bool,
    description_vars: dict[str, Any] | None = None,
    output_pydantic_by_task: dict[str, type] | None = None,
) -> list[Task]:
    """Build ordered tasks from CrewBase-style tasks.yaml.

    When ``assign_agents`` is False (hierarchical mode), tasks omit fixed
    ``agent=`` so CrewAI's manager decides who runs each task.
    """
    description_vars = description_vars or {}
    output_pydantic_by_task = output_pydantic_by_task or {}
    built: dict[str, Task] = {}

    for name, raw in tasks_config.items():
        config = dict(raw)
        agent_key = config.pop("agent", None)
        context_keys = config.pop("context", None) or []
        config.pop("tools", None)
        config.pop("output_pydantic", None)
        config.pop("output_json", None)

        description = config.get("description", "")
        if not isinstance(description, str):
            raise ValueError(f"Task {name!r} description must be a string")
        if description_vars:
            description = description.format_map(_FormatMap(description_vars))
            config["description"] = description

        expected_output = config.get("expected_output", "")
        if not isinstance(expected_output, str):
            raise ValueError(f"Task {name!r} expected_output must be a string")

        context_tasks: list[Task] = []
        for context_key in context_keys:
            if context_key not in built:
                raise KeyError(
                    f"Task {name!r} context references unknown or later task "
                    f"{context_key!r}"
                )
            context_tasks.append(built[context_key])

        agent = None
        if assign_agents:
            if not isinstance(agent_key, str) or agent_key not in agents:
                raise KeyError(f"Task {name!r} references unknown agent {agent_key!r}")
            agent = agents[agent_key]

        built[name] = Task(
            description=description,
            expected_output=expected_output,
            config=config,
            agent=agent,
            context=context_tasks or None,
            output_pydantic=output_pydantic_by_task.get(name),
        )

    return list(built.values())


def build_crew(
    *,
    agents: dict[str, Agent],
    tasks: list[Task],
    crew_config: dict[str, Any],
) -> Crew:
    """Assemble a Crew with sequential/hierarchical process from crew_config."""
    if is_hierarchical(crew_config):
        return Crew(
            agents=list(agents.values()),
            tasks=tasks,
            process=Process.hierarchical,
            manager_llm=manager_llm(crew_config),
            verbose=False,
        )
    return Crew(
        agents=list(agents.values()),
        tasks=tasks,
        process=Process.sequential,
        verbose=False,
    )
