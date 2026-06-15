from __future__ import annotations

import argparse
from pathlib import Path

from crewai_headless_flow.vendor_refresh import (
    discover_vendored_skill_names,
    refresh_vendored_skills,
    resolve_skill_names,
    validate_commit_sha,
    vendor_paths,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh vendored agent-skills from a pinned upstream commit."
    )
    parser.add_argument(
        "--commit",
        required=True,
        help="Full 40-character upstream git SHA to vendor from.",
    )
    parser.add_argument(
        "--date",
        help="Pinned date to record in NOTICE (defaults to today).",
    )
    parser.add_argument(
        "--skill",
        action="append",
        default=[],
        help="Additional skill directory name to vendor. Repeats allowed.",
    )
    parser.add_argument(
        "--drop-skill",
        action="append",
        default=[],
        help="Vendored skill directory name to remove from the final vendored set. Repeats allowed.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the final vendored skill set without writing files.",
    )
    parser.add_argument(
        "--repo-root",
        default=Path(__file__).resolve().parents[1],
        type=Path,
        help="Repository root containing vendor/agent-skills, NOTICE, and DESIGN.md.",
    )
    args = parser.parse_args()

    commit = validate_commit_sha(args.commit)
    paths = vendor_paths(args.repo_root)
    current_skills = discover_vendored_skill_names(paths.vendor_skills_root)
    requested_skills = resolve_skill_names(
        current_skills,
        add_skill_names=args.skill,
        drop_skill_names=args.drop_skill,
    )
    added_skills = sorted(set(requested_skills) - set(current_skills))
    removed_skills = sorted(set(current_skills) - set(requested_skills))

    if args.dry_run:
        print(f"Dry run for commit {commit}:")
        print("Current skills:")
        for skill_name in current_skills:
            print(f"- {skill_name}")
        print("Target skills:")
        for skill_name in requested_skills:
            print(f"- {skill_name}")
        if added_skills:
            print("Skills to add:")
            for skill_name in added_skills:
                print(f"- {skill_name}")
        if removed_skills:
            print("Skills to remove:")
            for skill_name in removed_skills:
                print(f"- {skill_name}")
        return 0

    refreshed = refresh_vendored_skills(
        repo_root=args.repo_root,
        commit=commit,
        pinned_date=args.date,
        skill_names=requested_skills,
        prune_missing=True,
    )

    print(f"Refreshed {len(refreshed)} skills from commit {commit}:")
    for skill_name in refreshed:
        print(f"- {skill_name}")
    if removed_skills:
        print("Removed skills:")
        for skill_name in removed_skills:
            print(f"- {skill_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
