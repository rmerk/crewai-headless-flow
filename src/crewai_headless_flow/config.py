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


def _discover_config_dir() -> Path:
    """
    Try to find the config directory in several reasonable places:
    1. Next to the project root (when running from source checkout)
    2. Current working directory / config
    3. Package data (future)
    """
    # When running from the source tree: .../CrewAI/config
    here = Path(__file__).resolve()
    candidates = [
        here.parents[3] / "config",  # typical src layout
        Path.cwd() / "config",
        Path.cwd().parent / "config",
    ]
    for cand in candidates:
        if (cand / "skills.yaml").exists() and (cand / "worker.yaml").exists():
            return cand

    # Fallback (will fail loudly with a clear message later)
    return here.parents[3] / "config"


DEFAULT_CONFIG_DIR = _discover_config_dir()
HUMAN_FEEDBACK_BOOLEAN_KEYS = {"enabled", "before_do_work", "before_finalize"}


def _validate_human_feedback(raw: dict[str, Any] | None) -> dict[str, Any]:
    human_feedback = raw or {"enabled": False}
    for key in HUMAN_FEEDBACK_BOOLEAN_KEYS:
        if key in human_feedback and not isinstance(human_feedback[key], bool):
            value_type = type(human_feedback[key]).__name__
            raise ValueError(
                f"human_feedback.{key} must be a boolean, got {value_type}"
            )
    return human_feedback


@dataclass
class StageConfig:
    stage: str
    skill: str
    worker: str  # "codex" | "grok" | "claude"
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

        print("=" * 72)

        # Human feedback status
        hf = self.human_feedback
        enabled = hf.get("enabled", False)
        print(f"\nHuman Feedback: {'ENABLED' if enabled else 'disabled'}")
        if enabled:
            print(f"  - Before do_work: {hf.get('before_do_work', True)}")
            print(f"  - Before finalize: {hf.get('before_finalize', True)}")
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

    with open(worker_path) as f:
        worker_raw = yaml.safe_load(f) or {}

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
