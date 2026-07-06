"""
Milestone 4: Config system tests.

Verifies:
- YAML loads correctly
- Per-stage resolution works
- Changing worker.yaml would result in different worker choice (argv)
- The pretty mapping printer works
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from crewai_headless_flow.config import (
    _discover_config_dir,
    classify_stage_extra,
    load_config,
)
from crewai_headless_flow.runtime_overrides import load_runtime_config


pytestmark = pytest.mark.offline


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def sample_config_dir(tmp_path: Path) -> Path:
    """Create a minimal but realistic config directory for testing."""
    d = tmp_path / "config"
    d.mkdir()

    skills = {
        "stages": {
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
            "review": "code-review-and-quality",
            "finalize": "documentation-and-adrs",
        }
    }
    (d / "skills.yaml").write_text(yaml.safe_dump(skills))

    workers = {
        "defaults": {"worker": "codex", "timeout": 300},
        "stages": {
            "plan": {"worker": "codex"},
            "do_work": {"worker": "grok", "model": "grok-3-latest"},
            "review": {"worker": "codex", "sandbox": "read-only"},
            "finalize": {"worker": "codex"},
        },
    }
    (d / "worker.yaml").write_text(yaml.safe_dump(workers))

    return d


def test_loads_and_resolves_stages(sample_config_dir: Path):
    cfg = load_config(sample_config_dir)

    assert set(cfg.stages) == {"plan", "do_work", "review", "finalize"}

    do_work = cfg.get_stage("do_work")
    assert do_work.skill == "incremental-implementation"
    assert do_work.worker == "grok"
    assert do_work.model == "grok-3-latest"

    review = cfg.get_stage("review")
    assert review.worker == "codex"
    assert review.extra.get("sandbox") == "read-only"


def _stage_snapshot(cfg, stage: str) -> dict[str, object]:
    stage_cfg = cfg.get_stage(stage)
    return {
        "skill": stage_cfg.skill,
        "worker": stage_cfg.worker,
        "model": stage_cfg.model,
        "timeout": stage_cfg.timeout,
        "extra": stage_cfg.extra,
    }


def _normalize_do_work_lane_extra(extra: dict[str, object]) -> dict[str, object]:
    normalized = yaml.safe_load(yaml.safe_dump(extra))

    crew = normalized.get("crew")
    if isinstance(crew, dict) and crew.get("enabled") is False:
        normalized.pop("crew", None)

    parallel = normalized.get("parallel")
    if isinstance(parallel, dict) and parallel.get("enabled") is False:
        normalized.pop("parallel", None)

    replan = normalized.get("replan")
    if isinstance(replan, dict) and replan.get("enabled") is False:
        normalized.pop("replan", None)

    return normalized


def _effective_stage_lane_snapshot(cfg, stage: str) -> dict[str, object]:
    stage_cfg = cfg.get_stage(stage)
    runtime_knobs, _enforced_declarations, notes = classify_stage_extra(
        stage, stage_cfg.extra
    )
    return {
        "skill": stage_cfg.skill,
        "worker": stage_cfg.worker,
        "model": stage_cfg.model,
        "timeout": stage_cfg.timeout,
        "runtime_knobs": _normalize_do_work_lane_extra(runtime_knobs),
        "notes": notes,
    }


def test_review_crew_config_loads_through_stage_extra(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["review"]["crew"] = {
        "enabled": True,
        "process": "sequential",
        "llm": {
            "model": "ollama/llama3.2",
            "base_url": "http://localhost:11434",
            "temperature": 0.2,
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    crew_cfg = cfg.get_stage("review").extra["crew"]
    assert crew_cfg["enabled"] is True
    assert crew_cfg["process"] == "sequential"
    assert crew_cfg["llm"]["model"] == "ollama/llama3.2"
    assert crew_cfg["llm"]["temperature"] == 0.2


def test_plan_crew_config_loads_through_stage_extra(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["plan"]["crew"] = {
        "enabled": True,
        "process": "sequential",
        "llm": {
            "model": "ollama/llama3.2",
            "base_url": "http://localhost:11434",
            "temperature": 0.2,
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    crew_cfg = cfg.get_stage("plan").extra["crew"]
    assert crew_cfg["enabled"] is True
    assert crew_cfg["process"] == "sequential"
    assert crew_cfg["llm"]["model"] == "ollama/llama3.2"
    assert crew_cfg["llm"]["temperature"] == 0.2


def test_do_work_crew_config_loads_through_stage_extra(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["do_work"]["crew"] = {
        "enabled": True,
        "process": "sequential",
        "decomposition": {"enabled": True, "max_subtasks": 3},
        "llm": {
            "model": "ollama/llama3.2",
            "base_url": "http://localhost:11434",
            "temperature": 0.2,
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    crew_cfg = cfg.get_stage("do_work").extra["crew"]
    assert crew_cfg["enabled"] is True
    assert crew_cfg["process"] == "sequential"
    assert crew_cfg["decomposition"]["enabled"] is True
    assert crew_cfg["decomposition"]["max_subtasks"] == 3
    assert crew_cfg["llm"]["model"] == "ollama/llama3.2"
    assert crew_cfg["llm"]["temperature"] == 0.2


def test_do_work_parallel_planner_config_loads_through_stage_extra(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["do_work"]["parallel"] = {
        "enabled": True,
        "max_workers": 3,
        "planner": {"enabled": True},
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    parallel_cfg = cfg.get_stage("do_work").extra["parallel"]
    assert parallel_cfg["enabled"] is True
    assert parallel_cfg["max_workers"] == 3
    assert parallel_cfg["planner"]["enabled"] is True


def test_do_work_replan_config_loads_through_stage_extra(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["do_work"]["replan"] = {
        "enabled": True,
        "on_execution_failure": True,
        "on_cross_task_change": True,
        "on_ambiguous_success": True,
        "max_execution_replans": 2,
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    replan_cfg = cfg.get_stage("do_work").extra["replan"]
    assert replan_cfg["enabled"] is True
    assert replan_cfg["on_execution_failure"] is True
    assert replan_cfg["on_cross_task_change"] is True
    assert replan_cfg["on_ambiguous_success"] is True
    assert replan_cfg["max_execution_replans"] == 2


def test_unknown_stage_extra_key_in_worker_yaml_fails_closed(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["do_work"]["paralel"] = {"enabled": True}
    worker_file.write_text(yaml.safe_dump(data))

    with pytest.raises(ValueError, match="unsupported keys: paralel"):
        load_config(sample_config_dir)


def test_invalid_stage_extra_value_type_in_worker_yaml_fails_closed(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["do_work"]["parallel"] = {
        "enabled": True,
        "max_workers": "four",
    }
    worker_file.write_text(yaml.safe_dump(data))

    with pytest.raises(
        ValueError,
        match="worker.yaml stages.do_work.parallel.max_workers must be integer",
    ):
        load_config(sample_config_dir)


def test_do_work_always_approve_false_fails_closed(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["do_work"]["always_approve"] = False
    worker_file.write_text(yaml.safe_dump(data))

    with pytest.raises(
        ValueError,
        match="worker.yaml stages.do_work.always_approve must be set to true",
    ):
        load_config(sample_config_dir)


def test_review_sandbox_other_than_read_only_fails_closed(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["review"]["sandbox"] = "workspace-write"
    worker_file.write_text(yaml.safe_dump(data))

    with pytest.raises(
        ValueError,
        match="worker.yaml stages.review.sandbox must be set to 'read-only'",
    ):
        load_config(sample_config_dir)


def test_crew_process_invalid_value_fails_closed(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["plan"]["crew"] = {
        "enabled": True,
        "process": "parallel",
    }
    worker_file.write_text(yaml.safe_dump(data))

    with pytest.raises(
        ValueError,
        match=r"worker\.yaml stages\.plan\.crew\.process must be set to "
        r"'sequential' or 'hierarchical'",
    ):
        load_config(sample_config_dir)


def test_crew_process_hierarchical_is_accepted(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["plan"]["crew"] = {
        "enabled": True,
        "process": "hierarchical",
    }
    worker_file.write_text(yaml.safe_dump(data))

    config = load_config(sample_config_dir)
    crew_cfg = config.get_stage("plan").extra["crew"]

    assert crew_cfg["process"] == "hierarchical"


def test_crew_delegation_enabled_flag_is_accepted(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["review"]["crew"] = {
        "enabled": True,
        "process": "sequential",
        "delegation": {"enabled": True},
    }
    worker_file.write_text(yaml.safe_dump(data))

    config = load_config(sample_config_dir)
    crew_cfg = config.get_stage("review").extra["crew"]

    assert crew_cfg["delegation"] == {"enabled": True}


def test_crew_delegation_rejects_non_boolean_enabled(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["review"]["crew"] = {
        "enabled": True,
        "delegation": {"enabled": "yes"},
    }
    worker_file.write_text(yaml.safe_dump(data))

    with pytest.raises(
        ValueError,
        match="worker.yaml stages.review.crew.delegation.enabled must be boolean",
    ):
        load_config(sample_config_dir)


def test_crew_manager_llm_accepts_partial_override(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["review"]["crew"] = {
        "enabled": True,
        "process": "hierarchical",
        "manager": {"llm": {"model": "gpt-4o"}},
    }
    worker_file.write_text(yaml.safe_dump(data))

    config = load_config(sample_config_dir)
    crew_cfg = config.get_stage("review").extra["crew"]

    assert crew_cfg["manager"]["llm"]["model"] == "gpt-4o"


def test_crew_manager_rejects_unknown_keys(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["review"]["crew"] = {
        "enabled": True,
        "manager": {"agent": "custom"},
    }
    worker_file.write_text(yaml.safe_dump(data))

    with pytest.raises(
        ValueError,
        match="worker.yaml stages.review.crew.manager contains unsupported keys",
    ):
        load_config(sample_config_dir)


def test_worker_switch_changes_resolution_without_code_change(sample_config_dir: Path):
    """
    This is the core M4 claim: editing worker.yaml changes which CLI/argv
    will be built, with zero changes to Python source.
    """
    cfg = load_config(sample_config_dir)

    # Currently do_work uses grok
    assert cfg.get_stage("do_work").worker == "grok"

    # Simulate the user editing worker.yaml to switch only do_work to codex
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["do_work"]["worker"] = "codex"
    worker_file.write_text(yaml.safe_dump(data))

    # Re-load (in real app this would be a new process or explicit reload)
    cfg2 = load_config(sample_config_dir)
    assert cfg2.get_stage("do_work").worker == "codex"

    # Other stages unchanged
    assert cfg2.get_stage("review").worker == "codex"


def test_worker_yaml_can_select_claude_without_code_change(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["do_work"] = {
        "worker": "claude",
        "model": "sonnet",
        "timeout": 450,
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)
    do_work = cfg.get_stage("do_work")

    assert do_work.worker == "claude"
    assert do_work.model == "sonnet"
    assert do_work.timeout == 450


def test_worker_yaml_can_select_gemini_without_code_change(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["do_work"] = {
        "worker": "gemini",
        "model": "gemini-2.5-pro",
        "timeout": 420,
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)
    do_work = cfg.get_stage("do_work")

    assert do_work.worker == "gemini"
    assert do_work.model == "gemini-2.5-pro"
    assert do_work.timeout == 420


def test_worker_yaml_can_select_cursor_without_code_change(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["do_work"] = {
        "worker": "cursor",
        "model": "composer-2.5",
        "timeout": 420,
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)
    do_work = cfg.get_stage("do_work")

    assert do_work.worker == "cursor"
    assert do_work.model == "composer-2.5"
    assert do_work.timeout == 420


def test_print_mapping_runs_without_crashing(sample_config_dir: Path, capsys):
    cfg = load_config(sample_config_dir)
    cfg.print_mapping()

    out = capsys.readouterr().out
    assert "Resolved Stage Configuration" in out
    assert "do_work" in out
    assert "grok" in out or "codex" in out
    assert "declarations={'sandbox': 'read-only'}" in out


def test_default_config_loads_from_real_files():
    """Smoke test against the actual committed config files."""
    cfg = load_config()  # uses DEFAULT_CONFIG_DIR

    assert "plan" in cfg.stages
    assert "do_work" in cfg.stages

    # At minimum the structure must be valid
    for stage in cfg.stages:
        resolved = cfg.get_stage(stage)
        assert resolved.worker in {"codex", "grok", "claude", "gemini", "cursor"}


def test_discover_config_dir_falls_back_to_bundled_assets(tmp_path: Path):
    bundled = tmp_path / "bundled-config"
    bundled.mkdir()
    (bundled / "skills.yaml").write_text("stages: {}\n")
    (bundled / "worker.yaml").write_text("defaults: {}\nstages: {}\n")

    discovered = _discover_config_dir(
        source_config_dir=tmp_path / "missing-source-config",
        cwd=tmp_path / "workspace",
        bundled_config_dir=bundled,
    )

    assert discovered == bundled


def test_default_review_crew_is_configured_but_disabled():
    cfg = load_config()

    review = cfg.get_stage("review")
    crew_cfg = review.extra.get("crew")

    assert crew_cfg is not None
    assert crew_cfg["enabled"] is False
    assert crew_cfg["process"] == "sequential"
    assert crew_cfg["llm"]["model"] == "ollama/llama3.2"


def test_default_plan_crew_is_configured_but_disabled():
    cfg = load_config()

    plan = cfg.get_stage("plan")
    crew_cfg = plan.extra.get("crew")

    assert crew_cfg is not None
    assert crew_cfg["enabled"] is False
    assert crew_cfg["process"] == "sequential"
    assert crew_cfg["llm"]["model"] == "ollama/llama3.2"


def test_default_do_work_crew_is_configured_but_disabled():
    cfg = load_config()

    do_work = cfg.get_stage("do_work")
    crew_cfg = do_work.extra.get("crew")

    assert crew_cfg is not None
    assert crew_cfg["enabled"] is False
    assert crew_cfg["process"] == "sequential"
    assert crew_cfg["decomposition"]["enabled"] is False
    assert crew_cfg["decomposition"]["max_subtasks"] == 4
    assert crew_cfg["llm"]["model"] == "ollama/llama3.2"


def test_default_do_work_parallel_planner_is_configured_but_disabled():
    cfg = load_config()

    do_work = cfg.get_stage("do_work")
    parallel_cfg = do_work.extra.get("parallel")

    assert parallel_cfg is not None
    assert parallel_cfg["enabled"] is False
    assert parallel_cfg["max_workers"] == 2
    assert parallel_cfg["planner"]["enabled"] is False


def test_default_do_work_replan_is_configured_but_disabled():
    cfg = load_config()

    do_work = cfg.get_stage("do_work")
    replan_cfg = do_work.extra.get("replan")

    assert replan_cfg is not None
    assert replan_cfg["enabled"] is False
    assert replan_cfg["on_execution_failure"] is False
    assert replan_cfg["on_cross_task_change"] is False
    assert replan_cfg["on_ambiguous_success"] is False
    assert replan_cfg["max_execution_replans"] == 1


def test_valid_boolean_human_feedback_loads(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "before_plan": True,
        "before_do_work": True,
        "before_review": False,
        "after_review": True,
        "before_finalize": False,
        "capture_instructions": True,
        "advanced_actions": True,
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_plan"] is True
    assert cfg.human_feedback["before_do_work"] is True
    assert cfg.human_feedback["before_review"] is False
    assert cfg.human_feedback["after_review"] is True
    assert cfg.human_feedback["before_finalize"] is False
    assert cfg.human_feedback["capture_instructions"] is True
    assert cfg.human_feedback["advanced_actions"] is True


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("enabled", "human_feedback.enabled must be a boolean, got str"),
        ("before_plan", "human_feedback.before_plan must be a boolean, got str"),
        (
            "before_do_work",
            "human_feedback.before_do_work must be a boolean, got str",
        ),
        (
            "before_review",
            "human_feedback.before_review must be a boolean, got str",
        ),
        (
            "after_review",
            "human_feedback.after_review must be a boolean, got str",
        ),
        (
            "before_finalize",
            "human_feedback.before_finalize must be a boolean, got str",
        ),
        (
            "capture_instructions",
            "human_feedback.capture_instructions must be a boolean, got str",
        ),
        (
            "advanced_actions",
            "human_feedback.advanced_actions must be a boolean, got str",
        ),
    ],
)
def test_human_feedback_known_keys_must_be_booleans(
    sample_config_dir: Path, key: str, message: str
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": False,
        "before_plan": False,
        "before_do_work": True,
        "before_review": False,
        "after_review": False,
        "before_finalize": True,
        "capture_instructions": False,
        "advanced_actions": False,
        key: "false",
    }
    worker_file.write_text(yaml.safe_dump(data))

    with pytest.raises(ValueError, match=message):
        load_config(sample_config_dir)


def test_human_feedback_unknown_keys_are_preserved(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": False,
        "future_gate": "allowed for later",
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["future_gate"] == "allowed for later"


def test_valid_action_allowlist_human_feedback_loads(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "do_work": ["skip-to-review"],
            "review": ["force-pass", "force-revise"],
            "finalize": ["skip-finalize", "rerun-review"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {
        "do_work": ["skip-to-review"],
        "review": ["force-pass", "force-revise"],
        "finalize": ["skip-finalize", "rerun-review"],
    }


def test_valid_gate_scoped_action_allowlist_human_feedback_loads(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "before_review": ["force-revise"],
            "after_review": ["replan", "target-tasks", "force-pass"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {
        "before_review": ["force-revise"],
        "after_review": ["replan", "target-tasks", "force-pass"],
    }


def test_before_review_only_force_revise_action_allowlist_loads(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "before_review": ["force-revise"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {"before_review": ["force-revise"]}


def test_before_review_only_force_pass_action_allowlist_loads(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "before_review": ["force-pass"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {"before_review": ["force-pass"]}


def test_after_review_rerun_review_action_allowlist_loads(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "after_review": ["rerun-review", "force-pass"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {
        "after_review": ["rerun-review", "force-pass"],
    }


def test_after_review_only_rerun_review_action_allowlist_loads(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "after_review": ["rerun-review"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {
        "after_review": ["rerun-review"],
    }


def test_after_review_only_target_tasks_action_allowlist_loads(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "after_review": ["target-tasks"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {
        "after_review": ["target-tasks"],
    }


def test_after_review_only_replan_action_allowlist_loads(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "after_review": ["replan"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {
        "after_review": ["replan"],
    }


def test_after_review_target_tasks_action_allowlist_loads(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "after_review": ["target-tasks", "force-pass"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {
        "after_review": ["target-tasks", "force-pass"],
    }


def test_after_review_force_revise_action_allowlist_loads(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "after_review": ["force-revise"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {
        "after_review": ["force-revise"],
    }


def test_after_review_force_pass_action_allowlist_loads(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "after_review": ["force-pass"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {
        "after_review": ["force-pass"],
    }


def test_human_feedback_action_allowlist_rejects_unsupported_action(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "review": ["skip-to-review"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    with pytest.raises(
        ValueError,
        match=(
            "human_feedback.action_allowlist.review contains unsupported actions: "
            "skip-to-review"
        ),
    ):
        load_config(sample_config_dir)


def test_human_feedback_action_allowlist_rejects_unsupported_gate_key(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "after_plan": ["replan"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    with pytest.raises(
        ValueError,
        match=(
            "human_feedback.action_allowlist.after_plan is unsupported. "
            "Supported stages or gates:"
        ),
    ):
        load_config(sample_config_dir)


def test_human_feedback_action_allowlist_rejects_rerun_review_on_before_review_gate(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "before_review": ["rerun-review"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    with pytest.raises(
        ValueError,
        match=(
            "human_feedback.action_allowlist.before_review contains unsupported actions: "
            "rerun-review"
        ),
    ):
        load_config(sample_config_dir)


def test_human_feedback_action_allowlist_accepts_target_tasks_on_before_review_gate(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "before_review": ["target-tasks"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {"before_review": ["target-tasks"]}


def test_human_feedback_action_allowlist_accepts_replan_on_before_review_gate(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "before_review": ["replan"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {"before_review": ["replan"]}


def test_human_feedback_action_allowlist_accepts_target_tasks_on_before_do_work_gate(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "before_do_work": ["target-tasks"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {
        "before_do_work": ["target-tasks"]
    }


def test_human_feedback_action_allowlist_accepts_replan_on_before_do_work_gate(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "before_do_work": ["replan"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {"before_do_work": ["replan"]}


def test_human_feedback_action_allowlist_accepts_skip_to_review_on_before_do_work_gate(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "before_do_work": ["skip-to-review"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {
        "before_do_work": ["skip-to-review"]
    }


def test_human_feedback_action_allowlist_accepts_rerun_review_on_before_finalize_gate(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "before_finalize": ["rerun-review"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {
        "before_finalize": ["rerun-review"]
    }


def test_human_feedback_action_allowlist_accepts_target_tasks_on_before_finalize_gate(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "before_finalize": ["target-tasks"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {
        "before_finalize": ["target-tasks"]
    }


def test_human_feedback_action_allowlist_accepts_replan_on_before_finalize_gate(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "before_finalize": ["replan"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {"before_finalize": ["replan"]}


def test_human_feedback_action_allowlist_accepts_force_revise_on_before_finalize_gate(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "before_finalize": ["force-revise"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {
        "before_finalize": ["force-revise"]
    }


def test_human_feedback_action_allowlist_accepts_skip_finalize_on_before_finalize_gate(
    sample_config_dir: Path,
):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "action_allowlist": {
            "before_finalize": ["skip-finalize"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["action_allowlist"] == {
        "before_finalize": ["skip-finalize"]
    }


def test_human_feedback_defaults_keep_new_gates_disabled(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {"enabled": True}
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_plan"] is False
    assert cfg.human_feedback["before_do_work"] is True
    assert cfg.human_feedback["before_review"] is False
    assert cfg.human_feedback["after_review"] is False
    assert cfg.human_feedback["before_finalize"] is True
    assert cfg.human_feedback["capture_instructions"] is False
    assert cfg.human_feedback["advanced_actions"] is False
    assert cfg.human_feedback["action_allowlist"] == {}


def test_example_config_claude_do_work_loads():
    config_dir = (
        Path(__file__).resolve().parents[2] / "examples" / "configs" / "claude-do-work"
    )

    cfg = load_config(config_dir)

    assert cfg.get_stage("do_work").worker == "claude"
    assert cfg.get_stage("do_work").model == "sonnet"
    assert cfg.human_feedback["enabled"] is False


def test_example_config_operator_review_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "operator-review-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_review"] is False
    assert cfg.human_feedback["after_review"] is True
    assert cfg.human_feedback["action_allowlist"] == {
        "after_review": ["rerun-review", "force-revise", "force-pass"]
    }


def test_example_config_review_rerun_review_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "review-rerun-review-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_review"] is False
    assert cfg.human_feedback["after_review"] is True
    assert cfg.human_feedback["action_allowlist"] == {"after_review": ["rerun-review"]}


def test_example_config_review_targeting_only_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "review-targeting-only-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_review"] is False
    assert cfg.human_feedback["after_review"] is True
    assert cfg.human_feedback["action_allowlist"] == {"after_review": ["target-tasks"]}


def test_example_config_review_replan_only_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "review-replan-only-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_review"] is False
    assert cfg.human_feedback["after_review"] is True
    assert cfg.human_feedback["action_allowlist"] == {"after_review": ["replan"]}


def test_example_config_review_force_revise_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "review-force-revise-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_review"] is False
    assert cfg.human_feedback["after_review"] is True
    assert cfg.human_feedback["action_allowlist"] == {"after_review": ["force-revise"]}


def test_example_config_review_force_pass_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "review-force-pass-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_review"] is False
    assert cfg.human_feedback["after_review"] is True
    assert cfg.human_feedback["action_allowlist"] == {"after_review": ["force-pass"]}


def test_example_config_review_targeting_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "review-targeting-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_review"] is False
    assert cfg.human_feedback["after_review"] is True
    assert cfg.human_feedback["action_allowlist"] == {
        "after_review": [
            "rerun-review",
            "target-tasks",
            "force-revise",
            "force-pass",
        ]
    }


def test_example_config_review_targeting_before_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "review-targeting-before-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_review"] is True
    assert cfg.human_feedback["after_review"] is False
    assert cfg.human_feedback["action_allowlist"] == {"before_review": ["target-tasks"]}


def test_example_config_review_force_revise_before_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "review-force-revise-before-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_review"] is True
    assert cfg.human_feedback["after_review"] is False
    assert cfg.human_feedback["action_allowlist"] == {"before_review": ["force-revise"]}


def test_example_config_review_force_pass_before_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "review-force-pass-before-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_review"] is True
    assert cfg.human_feedback["after_review"] is False
    assert cfg.human_feedback["action_allowlist"] == {"before_review": ["force-pass"]}


def test_example_config_review_replan_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "review-replan-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_review"] is False
    assert cfg.human_feedback["after_review"] is True
    assert cfg.human_feedback["action_allowlist"] == {
        "after_review": [
            "replan",
            "rerun-review",
            "force-revise",
            "force-pass",
        ]
    }


def test_example_config_review_replan_before_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "review-replan-before-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_review"] is True
    assert cfg.human_feedback["after_review"] is False
    assert cfg.human_feedback["action_allowlist"] == {"before_review": ["replan"]}


def test_example_config_plan_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2] / "examples" / "configs" / "plan-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_plan"] is True
    assert cfg.human_feedback["before_do_work"] is False
    assert cfg.human_feedback["before_review"] is False
    assert cfg.human_feedback["after_review"] is False
    assert cfg.human_feedback["before_finalize"] is False
    assert cfg.human_feedback["capture_instructions"] is True
    assert cfg.human_feedback["action_allowlist"] == {}


def test_example_config_do_work_replan_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "do-work-replan-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_plan"] is False
    assert cfg.human_feedback["before_do_work"] is True
    assert cfg.human_feedback["before_review"] is False
    assert cfg.human_feedback["after_review"] is False
    assert cfg.human_feedback["before_finalize"] is False
    assert cfg.human_feedback["capture_instructions"] is False
    assert cfg.human_feedback["action_allowlist"] == {"before_do_work": ["replan"]}


def test_example_config_do_work_skip_to_review_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "do-work-skip-to-review-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_plan"] is False
    assert cfg.human_feedback["before_do_work"] is True
    assert cfg.human_feedback["before_review"] is False
    assert cfg.human_feedback["after_review"] is False
    assert cfg.human_feedback["before_finalize"] is False
    assert cfg.human_feedback["capture_instructions"] is False
    assert cfg.human_feedback["action_allowlist"] == {
        "before_do_work": ["skip-to-review"]
    }


def test_example_config_parallel_do_work_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "parallel-do-work"
    )

    cfg = load_config(config_dir)
    do_work = cfg.get_stage("do_work")

    assert cfg.human_feedback["enabled"] is False
    assert do_work.extra["parallel"]["enabled"] is True
    assert do_work.extra["parallel"]["max_workers"] == 4
    assert do_work.extra.get("replan", {}).get("enabled", False) is False
    assert do_work.extra["parallel"].get("planner", {}).get("enabled", False) is False


def test_example_config_parallel_planner_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "parallel-planner"
    )

    cfg = load_config(config_dir)
    do_work = cfg.get_stage("do_work")

    assert cfg.human_feedback["enabled"] is False
    assert do_work.extra["parallel"]["enabled"] is True
    assert do_work.extra["parallel"]["max_workers"] == 4
    assert do_work.extra["parallel"]["planner"]["enabled"] is True
    assert do_work.extra.get("replan", {}).get("enabled", False) is False


def test_example_config_implementation_crew_parallel_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "implementation-crew-parallel"
    )

    cfg = load_config(config_dir)
    do_work = cfg.get_stage("do_work")

    assert cfg.human_feedback["enabled"] is False
    assert do_work.extra["crew"]["enabled"] is True
    assert do_work.extra["parallel"]["enabled"] is True
    assert do_work.extra["parallel"]["max_workers"] == 4
    assert do_work.extra["parallel"]["planner"]["enabled"] is False
    assert do_work.extra["replan"]["enabled"] is False


def test_example_config_implementation_crew_parallel_planner_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "implementation-crew-parallel-planner"
    )

    cfg = load_config(config_dir)
    do_work = cfg.get_stage("do_work")

    assert cfg.human_feedback["enabled"] is False
    assert do_work.extra["crew"]["enabled"] is True
    assert do_work.extra["parallel"]["enabled"] is True
    assert do_work.extra["parallel"]["max_workers"] == 4
    assert do_work.extra["parallel"]["planner"]["enabled"] is True
    assert do_work.extra["replan"]["enabled"] is False


def test_example_config_implementation_crew_parallel_replan_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "implementation-crew-parallel-replan"
    )

    cfg = load_config(config_dir)
    do_work = cfg.get_stage("do_work")

    assert cfg.human_feedback["enabled"] is False
    assert do_work.extra["crew"]["enabled"] is True
    assert do_work.extra["parallel"]["enabled"] is True
    assert do_work.extra["parallel"]["max_workers"] == 4
    assert do_work.extra["parallel"]["planner"]["enabled"] is True
    assert do_work.extra["replan"]["enabled"] is True
    assert do_work.extra["replan"]["max_execution_replans"] == 2


@pytest.mark.parametrize(
    ("example_name", "stage_extra_overrides"),
    [
        (
            "implementation-crew",
            [
                "do_work.crew.enabled=true",
                "do_work.crew.decomposition.enabled=true",
                "do_work.crew.decomposition.max_subtasks=3",
            ],
        ),
        (
            "implementation-crew-parallel",
            [
                "do_work.crew.enabled=true",
                "do_work.crew.decomposition.enabled=true",
                "do_work.crew.decomposition.max_subtasks=3",
                "do_work.parallel.enabled=true",
                "do_work.parallel.max_workers=4",
            ],
        ),
        (
            "implementation-crew-parallel-planner",
            [
                "do_work.crew.enabled=true",
                "do_work.crew.decomposition.enabled=true",
                "do_work.crew.decomposition.max_subtasks=3",
                "do_work.parallel.enabled=true",
                "do_work.parallel.max_workers=4",
                "do_work.parallel.planner.enabled=true",
            ],
        ),
        (
            "implementation-crew-parallel-replan",
            [
                "do_work.crew.enabled=true",
                "do_work.crew.decomposition.enabled=true",
                "do_work.crew.decomposition.max_subtasks=3",
                "do_work.parallel.enabled=true",
                "do_work.parallel.max_workers=4",
                "do_work.parallel.planner.enabled=true",
                "do_work.replan.enabled=true",
                "do_work.replan.on_execution_failure=true",
                "do_work.replan.on_cross_task_change=true",
                "do_work.replan.on_ambiguous_success=true",
                "do_work.replan.max_execution_replans=2",
            ],
        ),
    ],
)
def test_documented_implementation_crew_override_recipes_match_example_packs(
    example_name: str,
    stage_extra_overrides: list[str],
):
    recipe_cfg = load_runtime_config(
        config_dir=REPO_ROOT / "config",
        stage_extra_overrides=stage_extra_overrides,
    )
    example_cfg = load_config(REPO_ROOT / "examples" / "configs" / example_name)

    assert recipe_cfg.skills == example_cfg.skills
    assert recipe_cfg.human_feedback == example_cfg.human_feedback
    assert _effective_stage_lane_snapshot(
        recipe_cfg, "do_work"
    ) == _effective_stage_lane_snapshot(example_cfg, "do_work")


@pytest.mark.parametrize(
    ("example_name", "stage", "runtime_kwargs"),
    [
        (
            "claude-do-work",
            "do_work",
            {
                "worker_overrides": ["do_work=claude"],
                "model_overrides": ["do_work=sonnet"],
            },
        ),
        (
            "gemini-do-work",
            "do_work",
            {
                "worker_overrides": ["do_work=gemini"],
                "model_overrides": ["do_work=gemini-2.5-pro"],
            },
        ),
        (
            "planning-crew",
            "plan",
            {
                "stage_extra_overrides": ["plan.crew.enabled=true"],
            },
        ),
        (
            "review-crew",
            "review",
            {
                "stage_extra_overrides": ["review.crew.enabled=true"],
            },
        ),
    ],
)
def test_documented_simple_override_recipes_match_example_packs(
    example_name: str,
    stage: str,
    runtime_kwargs: dict[str, list[str]],
):
    recipe_cfg = load_runtime_config(
        config_dir=REPO_ROOT / "config",
        **runtime_kwargs,
    )
    example_cfg = load_config(REPO_ROOT / "examples" / "configs" / example_name)

    assert recipe_cfg.skills == example_cfg.skills
    assert recipe_cfg.human_feedback == example_cfg.human_feedback
    assert _effective_stage_lane_snapshot(
        recipe_cfg, stage
    ) == _effective_stage_lane_snapshot(example_cfg, stage)


@pytest.mark.parametrize(
    ("example_name", "stage_extra_overrides"),
    [
        (
            "parallel-do-work",
            [
                "do_work.parallel.enabled=true",
                "do_work.parallel.max_workers=4",
            ],
        ),
        (
            "parallel-planner",
            [
                "do_work.parallel.enabled=true",
                "do_work.parallel.max_workers=4",
                "do_work.parallel.planner.enabled=true",
            ],
        ),
        (
            "parallel-replan",
            [
                "do_work.parallel.enabled=true",
                "do_work.parallel.max_workers=4",
                "do_work.parallel.planner.enabled=true",
                "do_work.replan.enabled=true",
                "do_work.replan.on_execution_failure=true",
                "do_work.replan.on_cross_task_change=true",
                "do_work.replan.on_ambiguous_success=true",
                "do_work.replan.max_execution_replans=2",
            ],
        ),
    ],
)
def test_documented_parallel_override_recipes_match_example_packs(
    example_name: str,
    stage_extra_overrides: list[str],
):
    recipe_cfg = load_runtime_config(
        config_dir=REPO_ROOT / "config",
        stage_extra_overrides=stage_extra_overrides,
    )
    example_cfg = load_config(REPO_ROOT / "examples" / "configs" / example_name)

    assert recipe_cfg.skills == example_cfg.skills
    assert recipe_cfg.human_feedback == example_cfg.human_feedback
    assert _effective_stage_lane_snapshot(
        recipe_cfg, "do_work"
    ) == _effective_stage_lane_snapshot(example_cfg, "do_work")


def test_example_config_guided_operator_loop_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "guided-operator-loop"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_do_work"] is True
    assert cfg.human_feedback["after_review"] is True
    assert cfg.human_feedback["before_finalize"] is True
    assert cfg.human_feedback["capture_instructions"] is True
    assert cfg.human_feedback["action_allowlist"] == {
        "do_work": ["replan", "skip-to-review", "target-tasks"],
        "after_review": [
            "replan",
            "rerun-review",
            "target-tasks",
            "force-revise",
            "force-pass",
        ],
        "finalize": [
            "skip-finalize",
            "rerun-review",
            "force-revise",
            "replan",
            "target-tasks",
        ],
    }


def test_example_config_finalize_review_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "finalize-review-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_do_work"] is False
    assert cfg.human_feedback["after_review"] is False
    assert cfg.human_feedback["before_finalize"] is True
    assert cfg.human_feedback["action_allowlist"] == {
        "before_finalize": [
            "rerun-review",
            "force-revise",
            "replan",
            "target-tasks",
            "skip-finalize",
        ]
    }


def test_example_config_finalize_rerun_review_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "finalize-rerun-review-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_do_work"] is False
    assert cfg.human_feedback["after_review"] is False
    assert cfg.human_feedback["before_finalize"] is True
    assert cfg.human_feedback["action_allowlist"] == {
        "before_finalize": ["rerun-review"]
    }


def test_example_config_finalize_replan_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "finalize-replan-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_do_work"] is False
    assert cfg.human_feedback["after_review"] is False
    assert cfg.human_feedback["before_finalize"] is True
    assert cfg.human_feedback["action_allowlist"] == {"before_finalize": ["replan"]}


def test_example_config_finalize_force_revise_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "finalize-force-revise-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_do_work"] is False
    assert cfg.human_feedback["after_review"] is False
    assert cfg.human_feedback["before_finalize"] is True
    assert cfg.human_feedback["action_allowlist"] == {
        "before_finalize": ["force-revise"]
    }


def test_example_config_finalize_skip_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "finalize-skip-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_do_work"] is False
    assert cfg.human_feedback["after_review"] is False
    assert cfg.human_feedback["before_finalize"] is True
    assert cfg.human_feedback["action_allowlist"] == {
        "before_finalize": ["skip-finalize"]
    }


def test_example_config_finalize_targeting_gate_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "finalize-targeting-gate"
    )

    cfg = load_config(config_dir)

    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_do_work"] is False
    assert cfg.human_feedback["after_review"] is False
    assert cfg.human_feedback["before_finalize"] is True
    assert cfg.human_feedback["action_allowlist"] == {
        "before_finalize": ["target-tasks"]
    }


def test_example_config_parallel_replan_loads():
    config_dir = (
        Path(__file__).resolve().parents[2] / "examples" / "configs" / "parallel-replan"
    )

    cfg = load_config(config_dir)
    do_work = cfg.get_stage("do_work")

    assert do_work.extra["parallel"]["enabled"] is True
    assert do_work.extra["parallel"]["planner"]["enabled"] is True
    assert do_work.extra["replan"]["enabled"] is True
    assert do_work.extra["replan"]["max_execution_replans"] == 2


def test_example_config_implementation_crew_loads():
    config_dir = (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "configs"
        / "implementation-crew"
    )

    cfg = load_config(config_dir)
    do_work = cfg.get_stage("do_work")

    assert do_work.extra["crew"]["enabled"] is True
    assert do_work.extra["crew"]["max_rounds"] == 2
    assert do_work.extra["crew"]["decomposition"]["enabled"] is True
    assert do_work.extra["crew"]["decomposition"]["max_subtasks"] == 3
