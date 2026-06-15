from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.offline


REPO_ROOT = Path(__file__).resolve().parents[2]
COMMIT = "6ce029897d2b794940325fc7148774a6ec51111c"


def test_refresh_agent_skills_dry_run_previews_final_set_without_writing(
    tmp_path: Path,
):
    repo_root = tmp_path / "repo"
    skills_root = repo_root / "vendor" / "agent-skills" / "skills"
    for skill_name in ["planning-and-task-breakdown", "incremental-implementation"]:
        skill_dir = skills_root / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"{skill_name}\n")

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/refresh_agent_skills.py",
            "--commit",
            COMMIT,
            "--repo-root",
            str(repo_root),
            "--skill",
            "test-driven-development",
            "--drop-skill",
            "incremental-implementation",
            "--dry-run",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    out = proc.stdout
    assert f"Dry run for commit {COMMIT}:" in out
    assert "Current skills:" in out
    assert "Target skills:" in out
    assert "Skills to add:" in out
    assert "Skills to remove:" in out
    assert "- planning-and-task-breakdown" in out
    assert "- test-driven-development" in out
    assert "- incremental-implementation" in out
    assert not (repo_root / "vendor" / "agent-skills" / "VENDOR_COMMIT").exists()
    assert (skills_root / "incremental-implementation" / "SKILL.md").exists()
