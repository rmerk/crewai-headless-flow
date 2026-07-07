"""
Configuration loader and resolver for the CrewAI Headless Flow.

Primary responsibilities for Milestone 4:
- Load skills.yaml and worker.yaml
- Resolve the combined view per stage: (skill_name, worker, model, flags)
- Provide a beautiful startup printout of the full mapping
- Allow tests to easily override / inspect what worker would be chosen
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .human_feedback_actions import (
    human_feedback_gate_default_enabled,
    human_feedback_gate_label,
    supported_human_feedback_gates,
    supported_human_feedback_action_targets,
    supported_human_feedback_actions,
)

SOURCE_TREE_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
BUNDLED_CONFIG_DIR = Path(__file__).resolve().parent / "_bundled" / "config"
OLLAMA_MODEL_PREFIX = "ollama/"
OLLAMA_BASE_URLS = {
    "http://localhost:11434",
    "http://127.0.0.1:11434",
}


def _discover_config_dir(
    *,
    source_config_dir: Path | None = None,
    cwd: Path | None = None,
    bundled_config_dir: Path | None = None,
) -> Path:
    """
    Try to find the config directory in several reasonable places:
    1. Next to the project root (when running from source checkout)
    2. Current working directory / config
    3. Bundled wheel data (when running from an installed package)
    """
    source_config_dir = source_config_dir or SOURCE_TREE_CONFIG_DIR
    cwd = cwd or Path.cwd()
    bundled_config_dir = bundled_config_dir or BUNDLED_CONFIG_DIR

    candidates = [
        source_config_dir,
        cwd / "config",
        cwd.parent / "config",
        bundled_config_dir,
    ]
    for cand in candidates:
        if (cand / "skills.yaml").exists() and (cand / "worker.yaml").exists():
            return cand

    # Fallback (will fail loudly with a clear message later)
    return bundled_config_dir if bundled_config_dir.exists() else source_config_dir


DEFAULT_CONFIG_DIR = _discover_config_dir()
DEFAULT_HUMAN_FEEDBACK_GATES: dict[str, bool] = {
    gate: human_feedback_gate_default_enabled(gate)
    for gate in supported_human_feedback_gates()
}
# Conditional HITL (see hitl_policy.py). ``mode: "static"`` is the historical
# behavior (gate booleans decide). ``mode: "conditional"`` ignores those booleans
# and fires from the deterministic triggers below. Trigger→gate mapping is
# hardcoded in hitl_policy, so it is intentionally absent from this schema.
HUMAN_FEEDBACK_MODES = ("static", "conditional")
DEFAULT_CONDITIONAL_TRIGGERS: dict[str, dict[str, Any]] = {
    "approaching_max_revisions": {"enabled": False, "within": 1},
    "repeated_task_failure": {"enabled": False, "min_attempts": 2},
}


def _default_conditional() -> dict[str, Any]:
    return {
        "triggers": {
            name: dict(cfg) for name, cfg in DEFAULT_CONDITIONAL_TRIGGERS.items()
        }
    }


DEFAULT_HUMAN_FEEDBACK = {
    "enabled": False,
    "mode": "static",
    **DEFAULT_HUMAN_FEEDBACK_GATES,
    "capture_instructions": False,
    "advanced_actions": False,
    "action_allowlist": {},
    "conditional": _default_conditional(),
}
HUMAN_FEEDBACK_BOOLEAN_KEYS = {
    "enabled",
    *supported_human_feedback_gates(),
    "capture_instructions",
    "advanced_actions",
}


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_number(value: Any) -> bool:
    return (isinstance(value, int) or isinstance(value, float)) and not isinstance(
        value, bool
    )


def _is_string(value: Any) -> bool:
    return isinstance(value, str)


def _is_string_or_none(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _is_true(value: Any) -> bool:
    return value is True


def _is_read_only_sandbox(value: Any) -> bool:
    return value == "read-only"


CREW_PROCESSES = ("sequential", "hierarchical")


def _is_valid_crew_process(value: Any) -> bool:
    return value in CREW_PROCESSES


def _llm_schema() -> dict[str, Any]:
    return {
        "model": _is_string_or_none,
        "base_url": _is_string,
        "temperature": _is_number,
    }


def _crew_schema() -> dict[str, Any]:
    return {
        "enabled": _is_bool,
        "process": _is_valid_crew_process,
        # Sequential-only: opts the crew's coordinator/decision agent into
        # allow_delegation=True (DelegateWorkTool / AskQuestionTool) instead
        # of only reading a frozen Task.context summary.
        "delegation": {
            "enabled": _is_bool,
        },
        # Hierarchical-only: LLM for the auto-created manager agent. Falls
        # back to the crew's own `llm` block when omitted. Delegation
        # reliability depends heavily on using a capable tool-calling model
        # here (small/local models are known to mis-format delegation tool
        # calls or have the manager skip delegation entirely).
        "manager": {
            "llm": _llm_schema(),
        },
        "llm": _llm_schema(),
    }


def _do_work_crew_schema() -> dict[str, Any]:
    schema = _crew_schema()
    schema.update(
        {
            "max_rounds": _is_int,
            "decomposition": {
                "enabled": _is_bool,
                "max_subtasks": _is_int,
            },
        }
    )
    return schema


STAGE_EXTRA_SCHEMAS: dict[str, dict[str, Any]] = {
    "plan": {
        "crew": _crew_schema(),
    },
    "do_work": {
        "always_approve": _is_true,
        "crew": _do_work_crew_schema(),
        "parallel": {
            "enabled": _is_bool,
            "max_workers": _is_int,
            "planner": {
                "enabled": _is_bool,
            },
        },
        "replan": {
            "enabled": _is_bool,
            "on_execution_failure": _is_bool,
            "on_cross_task_change": _is_bool,
            "on_ambiguous_success": _is_bool,
            "max_execution_replans": _is_int,
        },
    },
    "review": {
        "sandbox": _is_read_only_sandbox,
        "crew": _crew_schema(),
    },
    "finalize": {},
}
ENFORCED_STAGE_EXTRA_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "plan": (("crew", "process"),),
    "do_work": (("always_approve",), ("crew", "process")),
    "review": (("sandbox",), ("crew", "process")),
    "finalize": (),
}


# Per-trigger schemas for `human_feedback.conditional.triggers.*`. Unknown keys
# (e.g. a stray `gate`, which is hardcoded in hitl_policy, not configurable) are
# rejected by _validate_value_against_schema.
_CONDITIONAL_TRIGGER_SCHEMAS: dict[str, dict[str, Any]] = {
    "approaching_max_revisions": {"enabled": _is_bool, "within": _is_positive_int},
    "repeated_task_failure": {"enabled": _is_bool, "min_attempts": _is_positive_int},
}


def _validate_conditional(raw: Any) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        value_type = type(raw).__name__
        raise ValueError(
            f"human_feedback.conditional must be a mapping, got {value_type}"
        )
    unknown = sorted(set(raw) - {"triggers"})
    if unknown:
        unknown_text = ", ".join(unknown)
        raise ValueError(
            f"human_feedback.conditional contains unsupported keys: {unknown_text}. "
            "Supported keys: triggers"
        )

    triggers_raw = raw.get("triggers", {})
    if not isinstance(triggers_raw, dict):
        value_type = type(triggers_raw).__name__
        raise ValueError(
            f"human_feedback.conditional.triggers must be a mapping, got {value_type}"
        )
    unknown_triggers = sorted(set(triggers_raw) - set(_CONDITIONAL_TRIGGER_SCHEMAS))
    if unknown_triggers:
        supported = ", ".join(sorted(_CONDITIONAL_TRIGGER_SCHEMAS))
        unknown_text = ", ".join(unknown_triggers)
        raise ValueError(
            f"human_feedback.conditional.triggers contains unsupported triggers: "
            f"{unknown_text}. Supported triggers: {supported}"
        )

    triggers: dict[str, Any] = {}
    for name, schema in _CONDITIONAL_TRIGGER_SCHEMAS.items():
        override = triggers_raw.get(name, {})
        if not isinstance(override, dict):
            value_type = type(override).__name__
            raise ValueError(
                f"human_feedback.conditional.triggers.{name} must be a mapping, "
                f"got {value_type}"
            )
        merged = {**DEFAULT_CONDITIONAL_TRIGGERS[name], **override}
        _validate_value_against_schema(
            f"human_feedback.conditional.triggers.{name}", merged, schema
        )
        triggers[name] = merged
    return {"triggers": triggers}


def _validate_human_feedback(raw: dict[str, Any] | None) -> dict[str, Any]:
    human_feedback = {**DEFAULT_HUMAN_FEEDBACK, **(raw or {})}
    for key in HUMAN_FEEDBACK_BOOLEAN_KEYS:
        if key in human_feedback and not isinstance(human_feedback[key], bool):
            value_type = type(human_feedback[key]).__name__
            raise ValueError(
                f"human_feedback.{key} must be a boolean, got {value_type}"
            )
    mode = human_feedback.get("mode", "static")
    if mode not in HUMAN_FEEDBACK_MODES:
        supported = ", ".join(HUMAN_FEEDBACK_MODES)
        raise ValueError(
            f"human_feedback.mode must be one of {supported}, got {mode!r}"
        )
    human_feedback["mode"] = mode
    human_feedback["conditional"] = _validate_conditional(
        human_feedback.get("conditional")
    )
    human_feedback["action_allowlist"] = _validate_action_allowlist(
        human_feedback.get("action_allowlist")
    )
    return human_feedback


def _validate_action_allowlist(raw: Any) -> dict[str, list[str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        value_type = type(raw).__name__
        raise ValueError(
            f"human_feedback.action_allowlist must be a mapping, got {value_type}"
        )

    normalized: dict[str, list[str]] = {}
    supported_targets = set(supported_human_feedback_action_targets())
    for target, actions in raw.items():
        if not isinstance(target, str):
            value_type = type(target).__name__
            raise ValueError(
                f"human_feedback.action_allowlist keys must be strings, got {value_type}"
            )
        if target not in supported_targets:
            supported = ", ".join(sorted(supported_targets))
            raise ValueError(
                f"human_feedback.action_allowlist.{target} is unsupported. "
                f"Supported stages or gates: {supported}"
            )
        if not isinstance(actions, list) or any(
            not isinstance(action, str) for action in actions
        ):
            raise ValueError(
                f"human_feedback.action_allowlist.{target} must be a list of strings"
            )
        allowed_actions = set(supported_human_feedback_actions(target))
        unknown = [action for action in actions if action not in allowed_actions]
        if unknown:
            supported = ", ".join(sorted(allowed_actions))
            unknown_text = ", ".join(unknown)
            raise ValueError(
                f"human_feedback.action_allowlist.{target} contains unsupported actions: "
                f"{unknown_text}. Supported actions: {supported}"
            )
        normalized[target] = list(actions)

    return normalized


def _validator_description(validator: Any) -> str:
    if validator is _is_bool:
        return "boolean"
    if validator is _is_int:
        return "integer"
    if validator is _is_positive_int:
        return "a positive integer (>= 1)"
    if validator is _is_number:
        return "number"
    if validator is _is_string:
        return "string"
    if validator is _is_string_or_none:
        return "string or null"
    if validator is _is_true:
        return "set to true"
    if validator is _is_read_only_sandbox:
        return "set to 'read-only'"
    if validator is _is_valid_crew_process:
        supported = " or ".join(f"'{process}'" for process in CREW_PROCESSES)
        return f"set to {supported}"
    return "valid value"


def _validate_value_against_schema(path: str, value: Any, schema: Any) -> None:
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            value_type = type(value).__name__
            raise ValueError(f"{path} must be a mapping, got {value_type}")
        unknown = sorted(set(value) - set(schema))
        if unknown:
            known = ", ".join(sorted(schema))
            unknown_text = ", ".join(unknown)
            raise ValueError(
                f"{path} contains unsupported keys: {unknown_text}. "
                f"Supported keys: {known}"
            )
        for key, nested in value.items():
            _validate_value_against_schema(f"{path}.{key}", nested, schema[key])
        return

    if callable(schema) and not schema(value):
        expected = _validator_description(schema)
        if schema in {_is_true, _is_read_only_sandbox, _is_valid_crew_process}:
            raise ValueError(f"{path} must be {expected}, got {value!r}")
        value_type = type(value).__name__
        raise ValueError(f"{path} must be {expected}, got {value_type}")


def _deep_copy_structure(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _deep_copy_structure(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [_deep_copy_structure(item) for item in value]
    return value


_MISSING = object()


def _pop_nested_path(mapping: dict[str, Any], path: tuple[str, ...]) -> Any:
    cursor: dict[str, Any] = mapping
    parents: list[tuple[dict[str, Any], str]] = []
    for part in path[:-1]:
        nested = cursor.get(part)
        if not isinstance(nested, dict):
            return _MISSING
        parents.append((cursor, part))
        cursor = nested
    leaf = path[-1]
    if leaf not in cursor:
        return _MISSING
    value = cursor.pop(leaf)
    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            parent.pop(key, None)
        else:
            break
    return value


def _set_nested_path(
    mapping: dict[str, Any], path: tuple[str, ...], value: Any
) -> None:
    cursor = mapping
    for part in path[:-1]:
        nested = cursor.get(part)
        if not isinstance(nested, dict):
            nested = {}
            cursor[part] = nested
        cursor = nested
    cursor[path[-1]] = value


def crew_llm_requires_ollama(crew_cfg: dict[str, Any]) -> bool:
    llm_cfg = crew_cfg.get("llm", {}) or {}
    model = llm_cfg.get("model")
    base_url = llm_cfg.get("base_url")
    model_text = model.strip() if isinstance(model, str) else ""
    base_url_text = base_url.strip().rstrip("/") if isinstance(base_url, str) else ""

    if not model_text and not base_url_text:
        return True
    if model_text.startswith(OLLAMA_MODEL_PREFIX):
        return True
    if base_url_text in OLLAMA_BASE_URLS:
        return True
    return False


def classify_stage_extra(
    stage: str, extra: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    runtime_knobs = _deep_copy_structure(extra)
    enforced_declarations: dict[str, Any] = {}
    for path in ENFORCED_STAGE_EXTRA_PATHS.get(stage, ()):
        value = _pop_nested_path(runtime_knobs, path)
        if value is not _MISSING:
            _set_nested_path(enforced_declarations, path, value)

    notes: list[str] = []
    crew_cfg = extra.get("crew")
    if isinstance(crew_cfg, dict) and crew_cfg.get("enabled", False):
        provider = (
            "ollama-local" if crew_llm_requires_ollama(crew_cfg) else "external/custom"
        )
        notes.append(f"crew_llm_provider={provider}")

    return runtime_knobs, enforced_declarations, notes


def validate_stage_worker_configs(
    *,
    skills: dict[str, str],
    workers: dict[str, Any],
) -> None:
    stages = workers.get("stages", {})
    if not isinstance(stages, dict):
        value_type = type(stages).__name__
        raise ValueError(f"worker.yaml stages must be a mapping, got {value_type}")

    for stage, config in stages.items():
        if not isinstance(stage, str):
            value_type = type(stage).__name__
            raise ValueError(
                f"worker.yaml stage names must be strings, got {value_type}"
            )
        if stage not in skills:
            continue
        if not isinstance(config, dict):
            value_type = type(config).__name__
            raise ValueError(
                f"worker.yaml stages.{stage} must be a mapping, got {value_type}"
            )

        stage_schema = STAGE_EXTRA_SCHEMAS.get(stage, {})
        core_keys = {"worker", "model", "timeout"}
        allowed_keys = core_keys | set(stage_schema)
        unknown = sorted(set(config) - allowed_keys)
        if unknown:
            allowed = ", ".join(sorted(allowed_keys))
            unknown_text = ", ".join(unknown)
            raise ValueError(
                f"worker.yaml stages.{stage} contains unsupported keys: {unknown_text}. "
                f"Supported keys: {allowed}"
            )
        if "worker" in config and not _is_string(config["worker"]):
            value_type = type(config["worker"]).__name__
            raise ValueError(
                f"worker.yaml stages.{stage}.worker must be a string, got {value_type}"
            )
        if "model" in config and not _is_string_or_none(config["model"]):
            value_type = type(config["model"]).__name__
            raise ValueError(
                f"worker.yaml stages.{stage}.model must be a string or null, got {value_type}"
            )
        if "timeout" in config and not _is_int(config["timeout"]):
            value_type = type(config["timeout"]).__name__
            raise ValueError(
                f"worker.yaml stages.{stage}.timeout must be an integer, got {value_type}"
            )
        for key in stage_schema:
            if key in config:
                _validate_value_against_schema(
                    f"worker.yaml stages.{stage}.{key}",
                    config[key],
                    stage_schema[key],
                )


def get_stage_extra_schema(stage: str, path_parts: list[str]) -> Any:
    schema = STAGE_EXTRA_SCHEMAS.get(stage, {})
    traversed: list[str] = []
    cursor: Any = schema
    for part in path_parts:
        current_path = ".".join([stage, *traversed]) if traversed else stage
        if not isinstance(cursor, dict):
            raise ValueError(
                f"Unsupported stage extra path '{stage}.{'.'.join(path_parts)}'. "
                f"'{current_path}' is not a mapping."
            )
        if part not in cursor:
            supported = ", ".join(sorted(cursor))
            raise ValueError(
                f"Unsupported stage extra path '{stage}.{'.'.join(path_parts)}'. "
                f"'{part}' is not valid under '{current_path}'. Supported keys: {supported}"
            )
        cursor = cursor[part]
        traversed.append(part)
    return cursor


@dataclass
class StageConfig:
    stage: str
    skill: str
    worker: str  # "codex" | "grok" | "claude" | "gemini" | "cursor"
    model: Optional[str] = None
    timeout: int = 300
    extra: dict[str, Any] = field(default_factory=dict)


class FlowConfig:
    """
    Resolved configuration for the entire flow.
    """

    def __init__(
        self,
        skills: dict[str, str],
        workers: dict[str, dict],
        defaults: dict[str, Any],
        human_feedback: dict[str, Any] | None = None,
    ) -> None:
        self.skills = skills
        self.workers = workers
        self.defaults = defaults
        self.human_feedback = _validate_human_feedback(human_feedback)
        self._stage_cache: dict[str, StageConfig] = {}

    def get_stage(self, stage: str) -> StageConfig:
        if stage in self._stage_cache:
            return self._stage_cache[stage]

        if stage not in self.skills:
            raise KeyError(
                f"Unknown stage '{stage}'. Known stages: {list(self.skills.keys())}"
            )

        skill = self.skills[stage]
        worker_cfg = self.workers.get(stage, {}) or {}
        merged = {**self.defaults, **worker_cfg}

        cfg = StageConfig(
            stage=stage,
            skill=skill,
            worker=merged.get("worker", "codex"),
            model=merged.get("model"),
            timeout=merged.get("timeout", 300),
            extra={
                k: v
                for k, v in merged.items()
                if k not in {"worker", "model", "timeout"}
            },
        )
        self._stage_cache[stage] = cfg
        return cfg

    @property
    def stages(self) -> list[str]:
        return list(self.skills.keys())

    def print_mapping(self) -> None:
        """Pretty-print the full skill → stage → worker mapping at startup."""
        print("\n" + "=" * 72)
        print("crewai-headless-flow — Resolved Stage Configuration")
        print("=" * 72)

        header = f"{'Stage':<12} {'Skill':<32} {'Worker':<10} {'Model':<20}"
        print(header)
        print("-" * 72)

        for stage in self.stages:
            cfg = self.get_stage(stage)
            model = cfg.model or "(default)"
            print(f"{stage:<12} {cfg.skill:<32} {cfg.worker:<10} {model:<20}")
            runtime_knobs, enforced_declarations, notes = classify_stage_extra(
                stage, cfg.extra
            )
            if runtime_knobs:
                print(f"  knobs={runtime_knobs}")
            if enforced_declarations:
                print(f"  declarations={enforced_declarations}")
            if notes:
                print(f"  notes={notes}")

        print("=" * 72)

        # Human feedback status
        hf = self.human_feedback
        enabled = hf.get("enabled", False)
        print(f"\nHuman Feedback: {'ENABLED' if enabled else 'disabled'}")
        if enabled:
            for gate in supported_human_feedback_gates():
                print(
                    f"  - {human_feedback_gate_label(gate)}: "
                    f"{hf.get(gate, human_feedback_gate_default_enabled(gate))}"
                )
            print(f"  - Capture instructions: {hf.get('capture_instructions', False)}")
            print(f"  - Advanced actions: {hf.get('advanced_actions', False)}")
            if hf.get("action_allowlist"):
                print(f"  - Action allowlist: {hf['action_allowlist']}")
        print()


def load_config(config_dir: Optional[Path] = None) -> FlowConfig:
    """
    Load and resolve configuration from YAML files.
    """
    config_dir = config_dir or DEFAULT_CONFIG_DIR

    skills_path = config_dir / "skills.yaml"
    worker_path = config_dir / "worker.yaml"

    with open(skills_path) as f:
        skills_raw = yaml.safe_load(f) or {}
    skills = skills_raw.get("stages", {})
    if not isinstance(skills, dict):
        value_type = type(skills).__name__
        raise ValueError(f"skills.yaml stages must be a mapping, got {value_type}")

    with open(worker_path) as f:
        worker_raw = yaml.safe_load(f) or {}
    if not isinstance(worker_raw, dict):
        value_type = type(worker_raw).__name__
        raise ValueError(f"worker.yaml must be a mapping, got {value_type}")

    validate_stage_worker_configs(skills=skills, workers=worker_raw)

    defaults = worker_raw.get("defaults", {})
    workers = worker_raw.get("stages", {})
    human_feedback = worker_raw.get("human_feedback", {"enabled": False})

    return FlowConfig(
        skills=skills,
        workers=workers,
        defaults=defaults,
        human_feedback=human_feedback,
    )


# Convenience singleton for early use (will be replaced by proper DI later)
_default_config: Optional[FlowConfig] = None


def get_default_config() -> FlowConfig:
    global _default_config
    if _default_config is None:
        _default_config = load_config()
    return _default_config


def print_stage_mapping() -> None:
    """Public helper used at startup and in CLI."""
    get_default_config().print_mapping()
