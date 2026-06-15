from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.offline


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_readme_mentions_every_example_config_pack():
    readme = (REPO_ROOT / "README.md").read_text()
    config_names = sorted(
        path.name
        for path in (REPO_ROOT / "examples" / "configs").iterdir()
        if path.is_dir()
    )

    missing = [name for name in config_names if name not in readme]
    assert missing == []


def test_operator_playbook_mentions_every_example_config_pack():
    operator_playbook = (REPO_ROOT / "docs" / "operator-playbook.md").read_text()
    config_names = sorted(
        path.name
        for path in (REPO_ROOT / "examples" / "configs").iterdir()
        if path.is_dir()
    )

    missing = [name for name in config_names if name not in operator_playbook]
    assert missing == []


def test_readme_documents_programmatic_api_entrypoints():
    readme = (REPO_ROOT / "README.md").read_text()

    assert "from crewai_headless_flow import (" in readme
    assert "load_runtime_config," in readme
    assert "run_headless_flow," in readme
    assert "render_execution_report," in readme
