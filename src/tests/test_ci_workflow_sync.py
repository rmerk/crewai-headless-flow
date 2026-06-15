from __future__ import annotations

from pathlib import Path

import pytest
import yaml


pytestmark = pytest.mark.offline


REPO_ROOT = Path(__file__).resolve().parents[2]

CI_RUN_SNIPPETS = [
    "uv sync --all-extras --locked",
    "uv run python -m crewai_headless_flow --help",
    "uv run crewai-headless-flow --help",
    "uv run crewai-headless-flow preflight --target-repo . --format json",
    "uv build",
    'uv venv "$tmpdir/venv"',
    'uv pip install --python "$tmpdir/venv/bin/python" dist/*.whl',
    '"$tmpdir/venv/bin/crewai-headless-flow" --help',
    '"$tmpdir/venv/bin/python" -m crewai_headless_flow --help',
    "uv run ruff check .",
    "uv run ruff format --check .",
    "uv run mypy src",
    "uv run pytest -m offline --tb=short",
    "git diff --check",
]

README_CI_PHRASES = [
    "smokes both source-tree entrypoints (`python -m crewai_headless_flow` and "
    "`crewai-headless-flow`)",
    "validates `preflight`",
    "builds the wheel",
    "checks bundled wheel contents",
    "installs the built wheel into a clean venv",
    "smokes both packaged entrypoints",
    "runs `ruff check`, `ruff format --check`, `mypy src`, `pytest -m offline`, "
    "and `git diff --check`",
]

README_LOCAL_MIRROR_COMMANDS = [
    "uv run ruff check .",
    "uv run ruff format --check .",
    "uv run mypy src",
    "uv run pytest -m offline --tb=short",
    "git diff --check",
]

AGENTS_CI_PHRASES = [
    "Hermetic CI: `.github/workflows/ci.yml` uses locked `uv sync`, smokes both "
    "source-tree CLI entrypoints (`python -m crewai_headless_flow` and "
    "`crewai-headless-flow`), validates `preflight`, builds the wheel, checks "
    "bundled wheel contents, installs the built wheel into a clean venv, smokes "
    "both packaged entrypoints, then runs `ruff check`, `ruff format --check`, "
    "`mypy src`, `pytest -m offline`, and `git diff --check`.",
]


def _quality_job() -> dict[str, object]:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    )
    return workflow["jobs"]["quality"]


def test_ci_workflow_contains_documented_quality_gates():
    quality = _quality_job()
    steps = quality["steps"]
    step_names = [step["name"] for step in steps]
    run_blob = "\n".join(step.get("run", "") for step in steps)

    assert "Wheel content smoke" in step_names
    assert "Installed wheel smoke" in step_names
    for snippet in CI_RUN_SNIPPETS:
        assert snippet in run_blob


def test_readme_ci_contract_stays_in_sync_with_workflow():
    readme = (REPO_ROOT / "README.md").read_text()

    for phrase in README_CI_PHRASES:
        assert phrase in readme

    for command in README_LOCAL_MIRROR_COMMANDS:
        assert command in readme


def test_agents_ci_contract_stays_in_sync_with_workflow():
    agents = (REPO_ROOT / "AGENTS.md").read_text()

    for phrase in AGENTS_CI_PHRASES:
        assert phrase in agents

    assert "git diff --check" in agents
