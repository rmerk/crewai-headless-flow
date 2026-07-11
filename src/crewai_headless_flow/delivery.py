"""
Flow-owned git delivery (autonomy Gap 2 — commit, then push/PR behind the
verification gate).

A "completed" run used to leave the target repo's working tree dirty on
whatever branch it was on. When ``deliver.enabled`` is true, the Flow now
puts its own changes on a fresh ``{branch_prefix}{run_id}`` branch with a
commit, under hard guardrails:

- always a fresh branch — never a commit on the branch the operator was on;
- refuse if the computed branch name is in ``protected_branches``;
- stage only the flow's own changed files with per-path
  ``git add -- :(literal)<path>`` — never ``add -A``, because preflight
  tolerates pre-existing dirt with a warning and delivery must not launder
  it into the flow's commit. Paths are staged as ``:(literal)`` pathspecs
  (and leading-``:`` paths rejected) because changed-file names are
  worker-reported and untrusted: a name like ``*.py`` would otherwise be a
  glob that stages the operator's WIP;
- no destructive git anywhere (no ``--force``, no ``reset``, no branch
  deletion);
- ``deliver()`` never raises — failures come back as a ``failed`` report.

``push``/``pr`` (Phase 2) ship the committed branch off the machine, but
only when the caller vouches that the latest objective verification passed
(``verification_ok``) — otherwise they report ``blocked_unverified``. A
push or PR failure never demotes a successful commit: ``status`` stays
``committed`` and the failure is carried on the ``push``/``pr`` fields.
PRs go through the ``gh`` CLI via an injectable runner so the offline
suite never touches the network. Adapters keep zero git responsibility;
this module is the only git writer in the platform.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

GitRunner = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]

# "requested_not_implemented" is retired (nothing produces it anymore) but
# stays in the unions so Phase-1 state.json files still deserialize on resume.
PushStatus = Literal[
    "off", "pushed", "failed", "blocked_unverified", "requested_not_implemented"
]
PrStatus = Literal[
    "off",
    "created",
    "failed",
    "blocked_unverified",
    "skipped_no_push",
    "requested_not_implemented",
]

_BRANCH_COLLISION_LIMIT = 20
_IDENTITY_FALLBACK = ("crewai-headless-flow", "crewai-headless-flow@localhost")


class DeliveryReport(BaseModel):
    """Persisted outcome of the delivery step (rides on FlowState)."""

    status: Literal["skipped", "committed", "nothing_to_commit", "failed"]
    branch: str | None = None
    base_ref: str | None = None
    commit_sha: str | None = None
    staged_files: list[str] = Field(default_factory=list)
    skipped_files: list[str] = Field(default_factory=list)
    message: str = ""
    push: PushStatus = "off"
    pr: PrStatus = "off"
    pr_url: str | None = None


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def run_gh(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def deliver(
    cfg: Mapping[str, Any],
    *,
    target_repo: Path | str,
    changed_files: list[str],
    run_id: str | None,
    request: str,
    verification_ok: bool = True,
    verification_note: str | None = None,
    git: GitRunner = run_git,
    gh: GitRunner = run_gh,
) -> DeliveryReport:
    """Commit the flow's changed files onto a fresh branch. Never raises."""
    if not cfg.get("enabled", False):
        return DeliveryReport(status="skipped", message="deliver.enabled is false")

    target = Path(target_repo)
    try:
        return _deliver_enabled(
            cfg,
            target=target,
            changed_files=changed_files,
            run_id=run_id,
            request=request,
            verification_ok=verification_ok,
            verification_note=verification_note,
            git=git,
            gh=gh,
        )
    except Exception as exc:  # never let packaging kill a completed run
        return DeliveryReport(
            status="failed",
            message=f"Delivery failed unexpectedly: {type(exc).__name__}: {exc}",
        )


def _deliver_enabled(
    cfg: Mapping[str, Any],
    *,
    target: Path,
    changed_files: list[str],
    run_id: str | None,
    request: str,
    verification_ok: bool,
    verification_note: str | None,
    git: GitRunner,
    gh: GitRunner,
) -> DeliveryReport:
    def fail(message: str) -> DeliveryReport:
        logger.warning(f"[Delivery] {message}")
        return DeliveryReport(status="failed", message=message)

    probe = git(["rev-parse", "--is-inside-work-tree"], target)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return fail(
            "Target repo is not a git work tree: "
            + (probe.stderr or probe.stdout or "").strip()[:200]
        )

    base_ref = _resolve_base_ref(git, target)

    safe_files, skipped_files = _partition_safe_paths(changed_files, target)
    for skipped in skipped_files:
        logger.warning(f"[Delivery] Skipping unsafe changed-file path: {skipped!r}")
    if not safe_files:
        return DeliveryReport(
            status="nothing_to_commit",
            base_ref=base_ref,
            skipped_files=skipped_files,
            message="No safe changed files to stage.",
        )

    branch, branch_error = _allocate_branch(cfg, git, target, run_id)
    if branch is None:
        return fail(branch_error or "Could not allocate a delivery branch.")

    checkout = git(["checkout", "-b", branch], target)
    if checkout.returncode != 0:
        return fail(
            f"git checkout -b {branch} failed: {(checkout.stderr or '').strip()[:300]}"
        )

    if not cfg.get("commit", True):
        return DeliveryReport(
            status="committed",
            branch=branch,
            base_ref=base_ref,
            staged_files=[],
            skipped_files=skipped_files,
            message="Branch created; commit disabled by deliver.commit=false.",
        )

    add = git(["add", "--", *[f":(literal){path}" for path in safe_files]], target)
    if add.returncode != 0:
        return fail(f"git add failed: {(add.stderr or '').strip()[:300]}")

    staged = git(["diff", "--cached", "--name-only"], target)
    staged_files = [line for line in (staged.stdout or "").splitlines() if line]
    if not staged_files:
        return DeliveryReport(
            status="nothing_to_commit",
            branch=branch,
            base_ref=base_ref,
            skipped_files=skipped_files,
            message="Changed files matched the tree; nothing staged.",
        )

    commit_message = _build_commit_message(run_id, request, staged_files)
    identity_note = ""
    commit = git(["commit", "-m", commit_message], target)
    if commit.returncode != 0 and _is_missing_identity(commit.stderr or ""):
        commit = git(
            [
                "-c",
                f"user.name={_IDENTITY_FALLBACK[0]}",
                "-c",
                f"user.email={_IDENTITY_FALLBACK[1]}",
                "commit",
                "-m",
                commit_message,
            ],
            target,
        )
        identity_note = " (used fallback git identity)"
    if commit.returncode != 0:
        return fail(f"git commit failed: {(commit.stderr or '').strip()[:300]}")

    sha = git(["rev-parse", "HEAD"], target)
    commit_sha = sha.stdout.strip() if sha.returncode == 0 else None

    logger.info(
        f"[Delivery] Committed {len(staged_files)} file(s) on {branch}"
        f"{f' @ {commit_sha[:12]}' if commit_sha else ''}{identity_note}"
    )

    try:
        push_flag, pr_flag, pr_url, ship_notes = _ship(
            cfg,
            target=target,
            branch=branch,
            run_id=run_id,
            request=request,
            staged_files=staged_files,
            verification_ok=verification_ok,
            verification_note=verification_note,
            git=git,
            gh=gh,
        )
    except Exception as exc:
        # A ship crash must never demote the commit to status="failed" via
        # deliver()'s catch-all; the branch and commit exist and are usable.
        note = f"push/pr failed unexpectedly: {type(exc).__name__}: {exc}"
        logger.warning(f"[Delivery] {note}")
        push_flag = "failed" if cfg.get("push") else "off"
        pr_flag = "skipped_no_push" if cfg.get("pr") else "off"
        pr_url = None
        ship_notes = [note]

    message = f"Committed on fresh branch {branch}.{identity_note}"
    if ship_notes:
        message += " " + " ".join(ship_notes)
    return DeliveryReport(
        status="committed",
        branch=branch,
        base_ref=base_ref,
        commit_sha=commit_sha,
        staged_files=staged_files,
        skipped_files=skipped_files,
        message=message,
        push=push_flag,
        pr=pr_flag,
        pr_url=pr_url,
    )


def _ship(
    cfg: Mapping[str, Any],
    *,
    target: Path,
    branch: str,
    run_id: str | None,
    request: str,
    staged_files: list[str],
    verification_ok: bool,
    verification_note: str | None,
    git: GitRunner,
    gh: GitRunner,
) -> tuple[PushStatus, PrStatus, str | None, list[str]]:
    """Push the delivery branch and open a PR, gated on verification.

    A failure here never demotes the commit: it comes back as a
    ``failed`` push/pr flag plus a human-readable note.
    """

    if not cfg.get("push"):
        return "off", "off", None, []

    pr_requested = bool(cfg.get("pr"))
    if not verification_ok:
        note = (
            "push/pr blocked: the latest objective verification did not pass "
            "(see verify: config)."
        )
        logger.warning(f"[Delivery] {note}")
        return (
            "blocked_unverified",
            "blocked_unverified" if pr_requested else "off",
            None,
            [note],
        )

    remote = str(cfg.get("remote", "origin"))
    try:
        pushed = git(["push", "-u", remote, branch], target)
    except Exception as exc:
        note = f"git push failed: {type(exc).__name__}: {exc}"
        logger.warning(f"[Delivery] {note}")
        return "failed", "skipped_no_push" if pr_requested else "off", None, [note]
    if pushed.returncode != 0:
        note = f"git push failed: {(pushed.stderr or '').strip()[:300]}"
        logger.warning(f"[Delivery] {note}")
        return "failed", "skipped_no_push" if pr_requested else "off", None, [note]
    logger.info(f"[Delivery] Pushed {branch} to {remote}.")

    if not pr_requested:
        return "pushed", "off", None, []

    # No --base on purpose: the base ref may be whatever feature branch the
    # operator happened to be on; let gh target the repo's default branch.
    title = _build_commit_message(run_id, request, staged_files).splitlines()[0]
    body = _build_pr_body(run_id, request, staged_files, verification_note)
    try:
        created = gh(
            ["pr", "create", "--head", branch, "--title", title, "--body", body],
            target,
        )
    except Exception as exc:
        note = f"gh pr create failed: {type(exc).__name__}: {exc}"
        logger.warning(f"[Delivery] {note}")
        return "pushed", "failed", None, [note]
    if created.returncode != 0:
        note = f"gh pr create failed: {(created.stderr or '').strip()[:300]}"
        logger.warning(f"[Delivery] {note}")
        return "pushed", "failed", None, [note]

    pr_url = _extract_pr_url(created.stdout or "")
    logger.info(f"[Delivery] Opened PR: {pr_url or '(url not reported)'}")
    return "pushed", "created", pr_url, []


def _build_pr_body(
    run_id: str | None,
    request: str,
    staged_files: list[str],
    verification_note: str | None,
) -> str:
    file_lines = "\n".join(f"- {path}" for path in staged_files[:50])
    if len(staged_files) > 50:
        file_lines += f"\n- … and {len(staged_files) - 50} more"
    parts = [
        f"Automated change produced by crewai-headless-flow.\n\nRequest:\n{request}",
        f"Files changed by this run:\n{file_lines}",
    ]
    if verification_note:
        parts.append(verification_note)
    if run_id:
        parts.append(f"Run-Id: {run_id}")
    return "\n\n".join(parts)


def _extract_pr_url(stdout: str) -> str | None:
    for line in stdout.splitlines():
        candidate = line.strip()
        if candidate.startswith("http://") or candidate.startswith("https://"):
            return candidate
    return None


def _resolve_base_ref(git: GitRunner, target: Path) -> str:
    head = git(["symbolic-ref", "--short", "-q", "HEAD"], target)
    branch_name = head.stdout.strip()
    has_commits = git(["rev-parse", "--verify", "-q", "HEAD"], target)
    if has_commits.returncode != 0:
        return "(unborn)"
    if branch_name:
        return branch_name
    return "(detached)"


def _partition_safe_paths(
    changed_files: list[str], target: Path
) -> tuple[list[str], list[str]]:
    safe: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for rel_path in changed_files:
        if rel_path in seen:
            continue
        seen.add(rel_path)
        candidate = Path(rel_path)
        if rel_path.startswith(":"):
            # ":" starts git pathspec magic; even though staging wraps paths
            # in :(literal), a leading-":" name is never a real repo file the
            # flow produced — reject rather than trust it.
            skipped.append(rel_path)
            continue
        if candidate.is_absolute() or ".." in candidate.parts:
            skipped.append(rel_path)
            continue
        resolved = (target / candidate).resolve(strict=False)
        if not resolved.is_relative_to(target.resolve()):
            skipped.append(rel_path)
            continue
        safe.append(rel_path)
    return sorted(safe), sorted(skipped)


def _allocate_branch(
    cfg: Mapping[str, Any],
    git: GitRunner,
    target: Path,
    run_id: str | None,
) -> tuple[str | None, str | None]:
    prefix = str(cfg.get("branch_prefix", "flow/"))
    protected = {str(name) for name in cfg.get("protected_branches", [])}
    base_name = f"{prefix}{run_id or 'run'}"

    for attempt in range(1, _BRANCH_COLLISION_LIMIT + 1):
        candidate = base_name if attempt == 1 else f"{base_name}-{attempt}"
        if candidate in protected:
            return None, (
                f"Refusing to deliver to protected branch {candidate!r} "
                f"(deliver.protected_branches: {sorted(protected)})"
            )
        exists = git(["rev-parse", "--verify", "-q", f"refs/heads/{candidate}"], target)
        if exists.returncode != 0:
            return candidate, None
    return None, (
        f"Could not find a free branch name after {_BRANCH_COLLISION_LIMIT} "
        f"attempts (base: {base_name!r})"
    )


def _build_commit_message(
    run_id: str | None, request: str, staged_files: list[str]
) -> str:
    subject_request = " ".join(request.split())[:72] or "headless flow run"
    subject = f"flow({run_id or 'run'}): {subject_request}"
    file_lines = "\n".join(f"- {path}" for path in staged_files[:50])
    if len(staged_files) > 50:
        file_lines += f"\n- … and {len(staged_files) - 50} more"
    body = f"Files changed by this run:\n{file_lines}"
    trailer = f"Run-Id: {run_id}" if run_id else ""
    return "\n\n".join(part for part in (subject, body, trailer) if part)


def _is_missing_identity(stderr: str) -> bool:
    lowered = stderr.lower()
    return "tell me who you are" in lowered or "user.email" in lowered
