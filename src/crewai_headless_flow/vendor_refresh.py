"""Helpers for refreshing vendored agent-skills from a pinned upstream commit."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re
from urllib.request import urlopen


UPSTREAM_REPO_SLUG = "addyosmani/agent-skills"
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NOTICE_COMMIT_RE = re.compile(r"(^- Pinned commit: ).+$", re.MULTILINE)
NOTICE_DATE_RE = re.compile(r"(^- Date pinned: ).+$", re.MULTILINE)
NOTICE_FILES_RE = re.compile(r"(^- Files included: ).+$", re.MULTILINE)
DESIGN_COMMIT_RE = re.compile(r"(agent-skills\]\([^)]+\) at commit `)[^`]+(`)")


@dataclass(frozen=True)
class VendorPaths:
    repo_root: Path
    vendor_root: Path
    vendor_skills_root: Path
    vendor_commit_file: Path
    notice_file: Path
    design_file: Path


def vendor_paths(repo_root: Path) -> VendorPaths:
    repo_root = repo_root.resolve()
    vendor_root = repo_root / "vendor" / "agent-skills"
    return VendorPaths(
        repo_root=repo_root,
        vendor_root=vendor_root,
        vendor_skills_root=vendor_root / "skills",
        vendor_commit_file=vendor_root / "VENDOR_COMMIT",
        notice_file=repo_root / "NOTICE",
        design_file=repo_root / "DESIGN.md",
    )


def validate_commit_sha(commit: str) -> str:
    normalized = commit.strip()
    if not COMMIT_SHA_RE.fullmatch(normalized):
        raise ValueError(
            "Pinned agent-skills commit must be a full 40-character lowercase git SHA."
        )
    return normalized


def discover_vendored_skill_names(vendor_skills_root: Path) -> list[str]:
    skills = [
        path.parent.name
        for path in vendor_skills_root.glob("*/SKILL.md")
        if path.is_file()
    ]
    if not skills:
        raise ValueError(f"No vendored skills found under {vendor_skills_root}")
    return sorted(set(skills))


def resolve_skill_names(
    current_skill_names: Sequence[str],
    *,
    add_skill_names: Sequence[str] | None = None,
    drop_skill_names: Sequence[str] | None = None,
) -> list[str]:
    current = set(current_skill_names)
    additions = set(add_skill_names or [])
    removals = set(drop_skill_names or [])

    overlap = sorted(additions & removals)
    if overlap:
        raise ValueError(
            "Skill names cannot be both added and dropped in one refresh: "
            + ", ".join(overlap)
        )

    unknown_removals = sorted(removals - current)
    if unknown_removals:
        raise ValueError(
            "Cannot drop non-vendored skills: " + ", ".join(unknown_removals)
        )

    resolved = sorted((current | additions) - removals)
    if not resolved:
        raise ValueError("Final vendored skill set cannot be empty.")
    return resolved


def build_skill_raw_url(
    *,
    repo_slug: str,
    commit: str,
    skill_name: str,
) -> str:
    return (
        "https://raw.githubusercontent.com/"
        f"{repo_slug}/{commit}/skills/{skill_name}/SKILL.md"
    )


def fetch_skill_markdown(
    *,
    repo_slug: str,
    commit: str,
    skill_name: str,
) -> str:
    url = build_skill_raw_url(
        repo_slug=repo_slug,
        commit=commit,
        skill_name=skill_name,
    )
    with urlopen(url) as response:
        return response.read().decode("utf-8")


def update_notice_text(
    notice_text: str,
    *,
    commit: str,
    pinned_date: str,
    skill_count: int,
) -> str:
    updated = NOTICE_COMMIT_RE.sub(
        lambda match: f"{match.group(1)}{commit}", notice_text
    )
    updated = NOTICE_DATE_RE.sub(
        lambda match: f"{match.group(1)}{pinned_date}",
        updated,
    )
    updated = NOTICE_FILES_RE.sub(
        lambda match: (
            f"{match.group(1)}Only the SKILL.md files vendored under "
            "vendor/agent-skills/skills/ "
            f"(currently {skill_count} skills)"
        ),
        updated,
    )
    return updated


def update_design_text(design_text: str, *, commit: str) -> str:
    return DESIGN_COMMIT_RE.sub(
        lambda match: f"{match.group(1)}{commit}{match.group(2)}",
        design_text,
    )


def refresh_vendored_skills(
    *,
    repo_root: Path,
    commit: str,
    pinned_date: str | None = None,
    repo_slug: str = UPSTREAM_REPO_SLUG,
    skill_names: Sequence[str] | None = None,
    prune_missing: bool = False,
    fetcher: Callable[..., str] = fetch_skill_markdown,
) -> list[str]:
    commit = validate_commit_sha(commit)
    effective_date = pinned_date or date.today().isoformat()
    paths = vendor_paths(repo_root)
    skills = (
        sorted(set(skill_names))
        if skill_names is not None
        else discover_vendored_skill_names(paths.vendor_skills_root)
    )

    for skill_name in skills:
        destination = paths.vendor_skills_root / skill_name / "SKILL.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            fetcher(
                repo_slug=repo_slug,
                commit=commit,
                skill_name=skill_name,
            ),
            encoding="utf-8",
        )

    if prune_missing:
        keep = set(skills)
        for existing_dir in paths.vendor_skills_root.iterdir():
            if not existing_dir.is_dir():
                continue
            if existing_dir.name in keep:
                continue
            for child in existing_dir.iterdir():
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    for nested in sorted(child.rglob("*"), reverse=True):
                        if nested.is_file():
                            nested.unlink()
                        elif nested.is_dir():
                            nested.rmdir()
                    child.rmdir()
            existing_dir.rmdir()

    paths.vendor_commit_file.write_text(f"{commit}\n", encoding="utf-8")
    paths.notice_file.write_text(
        update_notice_text(
            paths.notice_file.read_text(encoding="utf-8"),
            commit=commit,
            pinned_date=effective_date,
            skill_count=len(skills),
        ),
        encoding="utf-8",
    )
    paths.design_file.write_text(
        update_design_text(
            paths.design_file.read_text(encoding="utf-8"),
            commit=commit,
        ),
        encoding="utf-8",
    )
    return skills
