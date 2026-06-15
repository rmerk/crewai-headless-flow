from __future__ import annotations

from pathlib import Path

import pytest

from crewai_headless_flow.vendor_refresh import (
    build_skill_raw_url,
    discover_vendored_skill_names,
    refresh_vendored_skills,
    resolve_skill_names,
    update_design_text,
    update_notice_text,
    validate_commit_sha,
    vendor_paths,
)


pytestmark = pytest.mark.offline


def test_validate_commit_sha_requires_full_lowercase_sha():
    commit = "6ce029897d2b794940325fc7148774a6ec51111c"

    assert validate_commit_sha(commit) == commit

    with pytest.raises(ValueError):
        validate_commit_sha("6ce0298")

    with pytest.raises(ValueError):
        validate_commit_sha(commit.upper())


def test_discover_vendored_skill_names_returns_sorted_unique_names(tmp_path: Path):
    root = tmp_path / "vendor" / "skills"
    for name in ["b-skill", "a-skill"]:
        skill_dir = root / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {name}\n")

    assert discover_vendored_skill_names(root) == ["a-skill", "b-skill"]


def test_resolve_skill_names_supports_add_and_drop():
    resolved = resolve_skill_names(
        ["planning-and-task-breakdown", "incremental-implementation"],
        add_skill_names=["test-driven-development"],
        drop_skill_names=["incremental-implementation"],
    )

    assert resolved == ["planning-and-task-breakdown", "test-driven-development"]


def test_resolve_skill_names_rejects_unknown_drop():
    with pytest.raises(
        ValueError, match="Cannot drop non-vendored skills: missing-skill"
    ):
        resolve_skill_names(
            ["planning-and-task-breakdown"],
            drop_skill_names=["missing-skill"],
        )


def test_build_skill_raw_url_uses_skills_path():
    assert build_skill_raw_url(
        repo_slug="addyosmani/agent-skills",
        commit="6ce029897d2b794940325fc7148774a6ec51111c",
        skill_name="planning-and-task-breakdown",
    ) == (
        "https://raw.githubusercontent.com/"
        "addyosmani/agent-skills/"
        "6ce029897d2b794940325fc7148774a6ec51111c/"
        "skills/planning-and-task-breakdown/SKILL.md"
    )


def test_update_notice_text_rewrites_commit_date_and_skill_count():
    notice = """- Pinned commit: old
- Date pinned: old-date
- Files included: old text
"""

    updated = update_notice_text(
        notice,
        commit="6ce029897d2b794940325fc7148774a6ec51111c",
        pinned_date="2026-06-14",
        skill_count=8,
    )

    assert "- Pinned commit: 6ce029897d2b794940325fc7148774a6ec51111c" in updated
    assert "- Date pinned: 2026-06-14" in updated
    assert "currently 8 skills" in updated


def test_update_design_text_rewrites_only_pinned_commit():
    design = (
        "We vendor a pinned snapshot of [addyosmani/agent-skills]"
        "(https://github.com/addyosmani/agent-skills) "
        "at commit `oldcommitoldcommitoldcommitoldcommitold`."
    )

    updated = update_design_text(
        design,
        commit="6ce029897d2b794940325fc7148774a6ec51111c",
    )

    assert "`6ce029897d2b794940325fc7148774a6ec51111c`" in updated


def test_refresh_vendored_skills_writes_skills_commit_notice_and_design(tmp_path: Path):
    repo_root = tmp_path / "repo"
    skills_root = repo_root / "vendor" / "agent-skills" / "skills"
    (skills_root / "planning-and-task-breakdown").mkdir(parents=True)
    (skills_root / "planning-and-task-breakdown" / "SKILL.md").write_text("old\n")
    (repo_root / "vendor" / "agent-skills").mkdir(parents=True, exist_ok=True)
    (repo_root / "vendor" / "agent-skills" / "VENDOR_COMMIT").write_text("old\n")
    (repo_root / "NOTICE").write_text(
        "- Pinned commit: old\n- Date pinned: old\n- Files included: old\n"
    )
    (repo_root / "DESIGN.md").write_text(
        "We vendor a pinned snapshot of [addyosmani/agent-skills]"
        "(https://github.com/addyosmani/agent-skills) at commit `oldoldoldoldoldoldoldoldoldoldoldoldoldold`."
    )

    def fake_fetcher(*, repo_slug: str, commit: str, skill_name: str) -> str:
        return f"{repo_slug}|{commit}|{skill_name}\n"

    refreshed = refresh_vendored_skills(
        repo_root=repo_root,
        commit="6ce029897d2b794940325fc7148774a6ec51111c",
        pinned_date="2026-06-14",
        fetcher=fake_fetcher,
    )

    assert refreshed == ["planning-and-task-breakdown"]
    assert (
        (skills_root / "planning-and-task-breakdown" / "SKILL.md").read_text()
        == "addyosmani/agent-skills|6ce029897d2b794940325fc7148774a6ec51111c|planning-and-task-breakdown\n"
    )
    assert (
        repo_root / "vendor" / "agent-skills" / "VENDOR_COMMIT"
    ).read_text() == "6ce029897d2b794940325fc7148774a6ec51111c\n"
    assert "2026-06-14" in (repo_root / "NOTICE").read_text()
    assert "currently 1 skills" in (repo_root / "NOTICE").read_text()
    assert (
        "`6ce029897d2b794940325fc7148774a6ec51111c`"
        in (repo_root / "DESIGN.md").read_text()
    )


def test_refresh_vendored_skills_prunes_removed_skill_dirs_when_requested(
    tmp_path: Path,
):
    repo_root = tmp_path / "repo"
    skills_root = repo_root / "vendor" / "agent-skills" / "skills"
    for skill_name in ["planning-and-task-breakdown", "incremental-implementation"]:
        skill_dir = skills_root / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"old-{skill_name}\n")
    (repo_root / "vendor" / "agent-skills").mkdir(parents=True, exist_ok=True)
    (repo_root / "vendor" / "agent-skills" / "VENDOR_COMMIT").write_text("old\n")
    (repo_root / "NOTICE").write_text(
        "- Pinned commit: old\n- Date pinned: old\n- Files included: old\n"
    )
    (repo_root / "DESIGN.md").write_text(
        "We vendor a pinned snapshot of [addyosmani/agent-skills]"
        "(https://github.com/addyosmani/agent-skills) at commit `oldoldoldoldoldoldoldoldoldoldoldoldoldold`."
    )

    def fake_fetcher(*, repo_slug: str, commit: str, skill_name: str) -> str:
        return f"{repo_slug}|{commit}|{skill_name}\n"

    refreshed = refresh_vendored_skills(
        repo_root=repo_root,
        commit="6ce029897d2b794940325fc7148774a6ec51111c",
        pinned_date="2026-06-15",
        skill_names=["planning-and-task-breakdown"],
        prune_missing=True,
        fetcher=fake_fetcher,
    )

    assert refreshed == ["planning-and-task-breakdown"]
    assert (skills_root / "planning-and-task-breakdown" / "SKILL.md").exists()
    assert not (skills_root / "incremental-implementation").exists()
    assert "currently 1 skills" in (repo_root / "NOTICE").read_text()


def test_repo_vendor_metadata_stays_in_sync():
    paths = vendor_paths(Path("."))
    commit = paths.vendor_commit_file.read_text().strip()
    skills = discover_vendored_skill_names(paths.vendor_skills_root)
    notice = paths.notice_file.read_text()
    design = paths.design_file.read_text()

    assert commit
    assert f"- Pinned commit: {commit}" in notice
    assert f"currently {len(skills)} skills" in notice
    assert f"at commit `{commit}`" in design
