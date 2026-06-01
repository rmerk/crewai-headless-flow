"""
Milestone 1: SkillLoader tests — fully offline, no network, no CLIs.

These tests must pass with `pytest -m offline`.
They prove:
- Skills are discovered from the filesystem (no hardcoding).
- Frontmatter is parsed correctly.
- Section extraction works on real skill heading styles.
- Unknown skills raise clear errors.
"""

from __future__ import annotations


import pytest

from crewai_headless_flow.skills.loader import SkillLoader, VENDOR_ROOT


pytestmark = pytest.mark.offline


def test_vendor_directory_exists():
    """Basic smoke that the vendored skills are present."""
    assert VENDOR_ROOT.exists(), f"Missing vendor skills dir: {VENDOR_ROOT}"
    assert (VENDOR_ROOT / "planning-and-task-breakdown" / "SKILL.md").exists()


def test_discovers_skills_from_filesystem():
    """The list must come from disk — never a hardcoded list."""
    loader = SkillLoader()
    skills = loader.list_skills()

    # We expect at least the 8 core skills used by the Flow
    expected_core = {
        "planning-and-task-breakdown",
        "spec-driven-development",
        "incremental-implementation",
        "test-driven-development",
        "source-driven-development",
        "code-review-and-quality",
        "doubt-driven-development",
        "documentation-and-adrs",
    }

    assert len(skills) >= 8, f"Expected at least 8 skills, got {len(skills)}: {skills}"
    assert expected_core.issubset(set(skills)), f"Missing core skills. Have: {skills}"


def test_parses_frontmatter_correctly():
    loader = SkillLoader()
    skill = loader.get_skill("planning-and-task-breakdown")

    assert skill.name == "planning-and-task-breakdown"
    assert "Breaks work into ordered tasks" in skill.description
    assert "SKILL.md" in str(skill.path)


def test_get_section_with_varied_headings():
    """
    Real skills use different headings:
    - "The Planning Process"
    - "The Gated Workflow"
    - "The Increment Cycle"
    etc.
    """
    loader = SkillLoader()

    # planning-and-task-breakdown uses "The Planning Process"
    planning = loader.get_section("planning-and-task-breakdown", "Process")
    assert "Plan Mode" in planning or "Dependency Graph" in planning
    assert len(planning) > 100

    # spec-driven-development uses "The Gated Workflow"
    spec = loader.get_section("spec-driven-development", "Gated")
    assert "SPECIFY" in spec or "Gated Workflow" in spec

    # incremental-implementation uses "The Increment Cycle"
    inc = loader.get_section("incremental-implementation", "Increment")
    assert "Implement" in inc and "Test" in inc


def test_get_core_guidance_fallback():
    """When no exact 'Process' heading, still returns useful procedural content."""
    loader = SkillLoader()
    guidance = loader.get_core_guidance("code-review-and-quality")

    assert len(guidance) > 200
    # Should contain real review guidance
    assert (
        "Correctness" in guidance
        or "Five-Axis" in guidance
        or "Review Process" in guidance
    )


def test_unknown_skill_raises_clear_error():
    loader = SkillLoader()

    with pytest.raises(KeyError) as exc:
        loader.get_skill("does-not-exist")

    msg = str(exc.value)
    assert "Unknown skill" in msg
    assert "planning-and-task-breakdown" in msg  # shows available list


def test_print_discovered_does_not_crash(capsys):
    loader = SkillLoader()
    loader.print_discovered()
    captured = capsys.readouterr()
    assert "Discovered" in captured.out
    assert "planning-and-task-breakdown" in captured.out


def test_all_core_skills_have_usable_guidance():
    """Integration-style check: every core skill must yield non-trivial guidance."""
    loader = SkillLoader()
    core = [
        "planning-and-task-breakdown",
        "spec-driven-development",
        "incremental-implementation",
        "test-driven-development",
        "source-driven-development",
        "code-review-and-quality",
        "doubt-driven-development",
        "documentation-and-adrs",
    ]

    for name in core:
        guidance = loader.get_core_guidance(name)
        assert len(guidance) > 150, f"{name} produced very short guidance"
        assert "##" in guidance, f"{name} guidance has no Markdown headings"
