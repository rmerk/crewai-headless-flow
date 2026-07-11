"""Deny-path policy (autonomy Gap 8b) — Flow-owned write constraints.

Edit-mode workers run their CLIs fully bypassed — that is the product — so
write constraints are enforced at the boundaries the Flow owns: the parallel
mergeback (denied files are simply never copied out of the isolated
workspace) and the post-hoc snapshot diff for in-place edits (denied files
are restored in the live repo). ``do_work.isolation: copy`` gives serial
tasks the same pre-merge enforcement the parallel path has.

Glob semantics are deliberately broad for a safety control:
``fnmatch.fnmatchcase`` against the posix relpath, where ``*`` crosses
``/`` — so ``*.env`` denies ``sub/dir/x.env`` too. Pinned by tests; do not
"fix" this into a bypass.

Restore semantics and their honest limits:

- tracked file  -> ``git checkout -- <path>`` (content reverted to HEAD);
- untracked file the run created -> unlinked;
- untracked file that pre-existed the run -> left in place and reported
  unrestorable — deleting it would destroy operator data, and snapshots
  store hashes, not content. The clean containment is ``isolation: copy``,
  where denied files never reach the real repo at all.

``restore_denied_paths`` is, alongside ``delivery.py``, the only git writer
in the platform — scoped to ``git checkout -- :(literal)<denied path>``.
Paths are always passed to git as ``:(literal)`` pathspecs because they are
derived from worker activity and may be glob-shaped (a file literally named
``*.env`` must not expand into a checkout of every tracked .env file).
"""

from __future__ import annotations

import logging
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

from .delivery import GitRunner, run_git

logger = logging.getLogger(__name__)


def match_denied(changed_files: Sequence[str], deny: Sequence[str]) -> dict[str, str]:
    """Map each changed path to the first deny glob it matches."""
    matches: dict[str, str] = {}
    for rel_path in changed_files:
        posix = PurePosixPath(rel_path).as_posix()
        for pattern in deny:
            if fnmatchcase(posix, pattern):
                matches[rel_path] = pattern
                break
    return matches


def restore_denied_paths(
    repo: Path | str,
    paths: Sequence[str],
    *,
    created: Iterable[str] = (),
    git: GitRunner = run_git,
) -> list[str]:
    """Restore denied paths in the live repo; returns the unrestorable ones.

    ``created`` is the set of paths that did not exist before the run
    (derived from the workspace snapshot) — those are safe to unlink when
    untracked. An untracked path NOT in ``created`` pre-existed the run and
    is reported unrestorable rather than deleted.
    """
    repo = Path(repo)
    created_set = set(created)
    unrestorable: list[str] = []

    for rel_path in paths:
        candidate = Path(rel_path)
        suspicious = (
            rel_path.startswith(":")
            or candidate.is_absolute()
            or ".." in candidate.parts
        )
        if suspicious:
            # ":" starts git pathspec magic and absolute/".." paths escape the
            # repo. Snapshot-derived paths are never shaped like this, so a
            # hostile name is refused rather than handed to git or unlink.
            logger.warning(
                f"[Paths] Refusing to restore suspicious denied path {rel_path!r}."
            )
            unrestorable.append(rel_path)
            continue
        target = repo / rel_path
        # :(literal) so a glob-shaped name like "*.env" cannot fan out into a
        # checkout of every matching tracked file (operator WIP destruction).
        literal = f":(literal){rel_path}"
        tracked = git(["ls-files", "--", literal], repo)
        if tracked.returncode == 0 and (tracked.stdout or "").strip():
            restored = git(["checkout", "--", literal], repo)
            if restored.returncode != 0:
                logger.warning(
                    f"[Paths] Could not restore denied path {rel_path!r}: "
                    f"{(restored.stderr or '').strip()[:200]}"
                )
                unrestorable.append(rel_path)
            continue

        if rel_path in created_set:
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    f"[Paths] Could not remove denied path {rel_path!r}: {exc}"
                )
                unrestorable.append(rel_path)
            continue

        if target.exists():
            # Pre-existing untracked file the worker modified: snapshots hold
            # hashes, not content — deleting would destroy operator data.
            logger.warning(
                f"[Paths] Denied path {rel_path!r} is untracked and pre-existed "
                "the run; cannot restore its content (use do_work.isolation: "
                "copy for full containment)."
            )
            unrestorable.append(rel_path)
        # else: deleted pre-existing untracked file — nothing left to restore,
        # and nothing to restore it from; fall through to unrestorable.
        else:
            unrestorable.append(rel_path)
    return unrestorable
