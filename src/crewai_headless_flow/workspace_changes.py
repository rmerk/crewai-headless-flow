"""Workspace snapshotting and isolated-change merge helpers."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path


IGNORED_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}


def create_workspace_copy(src: Path, *, prefix: str = "flow-parallel-") -> Path:
    tmp_root = Path(tempfile.mkdtemp(prefix=prefix))
    dst = tmp_root / src.name
    shutil.copytree(
        src,
        dst,
        symlinks=False,
        ignore=_ignore_symlinks,
        ignore_dangling_symlinks=True,
    )
    return dst


def cleanup_workspace_copy(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path.parent, ignore_errors=True)


def snapshot_workspace(root: Path) -> dict[str, str]:
    root = Path(root)
    snapshot: dict[str, str] = {}

    for rel_path in _list_relevant_files(root):
        abs_path = root / rel_path
        if not abs_path.exists() or abs_path.is_symlink():
            continue
        snapshot[rel_path] = _hash_file(abs_path)

    return snapshot


def diff_workspace_snapshots(
    before: dict[str, str],
    after: dict[str, str],
) -> list[str]:
    changed: list[str] = []
    all_paths = sorted(set(before) | set(after))
    for rel_path in all_paths:
        if before.get(rel_path) != after.get(rel_path):
            changed.append(rel_path)
    return changed


def apply_changed_files(
    *,
    src_root: Path,
    dest_root: Path,
    changed_files: list[str],
) -> None:
    for rel_path in changed_files:
        src = src_root / rel_path
        dest = dest_root / rel_path

        if src.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            continue

        if dest.exists():
            dest.unlink()


def _list_relevant_files(root: Path) -> list[str]:
    git_files = _git_visible_files(root)
    if git_files is not None:
        return git_files

    files: list[str] = []
    for abs_path in root.rglob("*"):
        if abs_path.is_dir():
            continue
        if any(part in IGNORED_DIR_NAMES for part in abs_path.relative_to(root).parts):
            continue
        if abs_path.is_symlink():
            continue
        files.append(abs_path.relative_to(root).as_posix())
    return sorted(files)


def _git_visible_files(root: Path) -> list[str] | None:
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-co",
                "--exclude-standard",
                "-z",
            ],
            capture_output=True,
            text=False,
            timeout=10,
            check=False,
        )
    except Exception:
        return None

    if proc.returncode != 0:
        return None

    raw = proc.stdout.decode("utf-8", errors="ignore")
    paths = [item for item in raw.split("\x00") if item]
    return sorted(path for path in paths if path and not _contains_ignored_dir(path))


def _contains_ignored_dir(rel_path: str) -> bool:
    return any(part in IGNORED_DIR_NAMES for part in Path(rel_path).parts)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ignore_symlinks(directory: str, names: list[str]) -> list[str]:
    return [name for name in names if (Path(directory) / name).is_symlink()]
