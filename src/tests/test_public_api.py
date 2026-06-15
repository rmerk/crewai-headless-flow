from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import crewai_headless_flow as public_api
from crewai_headless_flow.config import DEFAULT_CONFIG_DIR, FlowConfig, load_config
from crewai_headless_flow.flow import (
    CrewAIHeadlessFlow,
    resume_headless_flow,
    run_headless_flow,
)
from crewai_headless_flow.reporting import render_execution_report
from crewai_headless_flow.runtime_overrides import load_runtime_config
from crewai_headless_flow.state import FlowState


pytestmark = pytest.mark.offline

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_public_api_exports_supported_programmatic_entrypoints():
    assert public_api.CrewAIHeadlessFlow is CrewAIHeadlessFlow
    assert public_api.DEFAULT_CONFIG_DIR == DEFAULT_CONFIG_DIR
    assert public_api.FlowConfig is FlowConfig
    assert public_api.FlowState is FlowState
    assert public_api.load_config is load_config
    assert public_api.load_runtime_config is load_runtime_config
    assert public_api.render_execution_report is render_execution_report
    assert public_api.resume_headless_flow is resume_headless_flow
    assert public_api.run_headless_flow is run_headless_flow
    assert isinstance(public_api.__version__, str)
    assert public_api.__version__


def test_load_runtime_config_supports_library_defaults():
    config_dir = REPO_ROOT / "config"
    cfg = load_runtime_config(config_dir=config_dir)

    assert cfg.skills == load_config(config_dir).skills
    assert cfg.get_stage("do_work").worker == "grok"
    assert cfg.human_feedback["enabled"] is False


def test_top_level_public_api_import_is_lazy_and_side_effect_free():
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, crewai_headless_flow; "
                "print('crewai_headless_flow.flow' in sys.modules)"
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    )

    assert proc.stdout.strip() == "False"
    assert "LiteLLM" not in proc.stderr
