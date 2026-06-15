from __future__ import annotations

import tomllib
from pathlib import Path

import pytest


pytestmark = pytest.mark.offline


def test_pyproject_declares_console_script_entrypoint():
    data = tomllib.loads(Path("pyproject.toml").read_text())

    assert data["project"]["scripts"]["crewai-headless-flow"] == (
        "crewai_headless_flow.cli:main"
    )


def test_pyproject_force_includes_bundled_config_and_vendor_skills():
    data = tomllib.loads(Path("pyproject.toml").read_text())
    force_include = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]

    assert force_include["config"] == "src/crewai_headless_flow/_bundled/config"
    assert force_include["vendor/agent-skills/skills"] == (
        "src/crewai_headless_flow/_bundled/vendor/agent-skills/skills"
    )
