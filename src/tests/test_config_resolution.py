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

from crewai_headless_flow.config import load_config


pytestmark = pytest.mark.offline


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


def test_print_mapping_runs_without_crashing(sample_config_dir: Path, capsys):
    cfg = load_config(sample_config_dir)
    cfg.print_mapping()

    out = capsys.readouterr().out
    assert "Resolved Stage Configuration" in out
    assert "do_work" in out
    assert "grok" in out or "codex" in out


def test_default_config_loads_from_real_files():
    """Smoke test against the actual committed config files."""
    cfg = load_config()  # uses DEFAULT_CONFIG_DIR

    assert "plan" in cfg.stages
    assert "do_work" in cfg.stages

    # At minimum the structure must be valid
    for stage in cfg.stages:
        resolved = cfg.get_stage(stage)
        assert resolved.worker in {"codex", "grok"}
