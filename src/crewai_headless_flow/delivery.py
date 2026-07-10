"""
Flow-owned git delivery (autonomy Phase 1, Gap 2 — commit-only mode).

A "completed" run used to leave the target repo's working tree dirty on
whatever branch it was on. When ``deliver.enabled`` is true, the Flow now
puts its own changes on a fresh ``{branch_prefix}{run_id}`` branch with a
commit, under hard guardrails:

- always a fresh branch — never a commit on the branch the operator was on;
- refuse if the computed branch name is in ``protected_branches``;
- stage only the flow's own changed files with per-path ``git add --`` —
  never ``add -A``, because preflight tolerates pre-existing dirt with a
  warning and delivery must not launder it into the flow's commit;
- no destructive git anywhere (no ``--force``, no ``reset``, no branch
  deletion);
- ``deliver()`` never raises — failures come back as a ``failed`` report.

``push`` / ``pr`` are validated config keys but deliberately not implemented
in Phase 1: shipping work off the machine waits for Phase 2's verification
gate. Adapters keep zero git responsibility; this module is the only git
writer in the platform.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel, Field

GitRunner = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]

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
    push: Literal["off", "requested_not_implemented"] = "off"
    pr: Literal["off", "requested_not_implemented"] = "off"


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def deliver(
    cfg: Mapping[str, Any],
    *,
    target_repo: Path | str,
    changed_files: list[str],
    run_id: str | None,
    request: str,
    git: GitRunner = run_git,
) -> DeliveryReport:
    """Commit the flow's changed files onto a fresh branch. Never raises."""
    push_flag: Literal["off", "requested_not_implemented"] = (
        "requested_not_implemented" if cfg.get("push") else "off"
    )
    pr_flag: Literal["off", "requested_not_implemented"] = (
        "requested_not_implemented" if cfg.get("pr") else "off"
    )
    if cfg.get("push"):
        print(
            "[Delivery] deliver.push=true is not implemented in Phase 1 "
            "(deferred to Phase 2 behind the verify gate); ignoring."
        )
    if cfg.get("pr"):
        print(
            "[Delivery] deliver.pr=true is not implemented in Phase 1 "
            "(deferred to Phase 2 behind the verify gate); ignoring."
        )

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
            git=git,
            push_flag=push_flag,
            pr_flag=pr_flag,
        )
    except Exception as exc:  # never let packaging kill a completed run
        return DeliveryReport(
            status="failed",
            message=f"Delivery failed unexpectedly: {type(exc).__name__}: {exc}",
            push=push_flag,
            pr=pr_flag,
        )


def _deliver_enabled(
    cfg: Mapping[str, Any],
    *,
    target: Path,
    changed_files: list[str],
    run_id: str | None,
    request: str,
    git: GitRunner,
    push_flag: Literal["off", "requested_not_implemented"],
    pr_flag: Literal["off", "requested_not_implemented"],
) -> DeliveryReport:
    def fail(message: str) -> DeliveryReport:
        print(f"[Delivery] {message}")
        return DeliveryReport(
            status="failed", message=message, push=push_flag, pr=pr_flag
        )

    probe = git(["rev-parse", "--is-inside-work-tree"], target)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return fail(
            "Target repo is not a git work tree: "
            + (probe.stderr or probe.stdout or "").strip()[:200]
        )

    base_ref = _resolve_base_ref(git, target)

    safe_files, skipped_files = _partition_safe_paths(changed_files, target)
    for skipped in skipped_files:
        print(f"[Delivery] Skipping unsafe changed-file path: {skipped!r}")
    if not safe_files:
        return DeliveryReport(
            status="nothing_to_commit",
            base_ref=base_ref,
            skipped_files=skipped_files,
            message="No safe changed files to stage.",
            push=push_flag,
            pr=pr_flag,
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
            push=push_flag,
            pr=pr_flag,
        )

    add = git(["add", "--", *safe_files], target)
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
            push=push_flag,
            pr=pr_flag,
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

    print(
        f"[Delivery] Committed {len(staged_files)} file(s) on {branch}"
        f"{f' @ {commit_sha[:12]}' if commit_sha else ''}{identity_note}"
    )
    return DeliveryReport(
        status="committed",
        branch=branch,
        base_ref=base_ref,
        commit_sha=commit_sha,
        staged_files=staged_files,
        skipped_files=skipped_files,
        message=f"Committed on fresh branch {branch}.{identity_note}",
        push=push_flag,
        pr=pr_flag,
    )


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
