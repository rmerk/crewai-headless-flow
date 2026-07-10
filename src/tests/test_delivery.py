"""
Gap 2 (autonomy Phase 1): Flow-owned git delivery, commit-only mode.

Branch/commit tests run real `git` against tmp_path repos — zero network,
consistent with the offline discipline. Failure-path tests inject a fake git
runner.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from crewai_headless_flow.config import FlowConfig
from crewai_headless_flow.delivery import DeliveryReport, deliver
from crewai_headless_flow.flow import CrewAIHeadlessFlow
from crewai_headless_flow.state import FlowState
from crewai_headless_flow.workers.base import CoderResult


pytestmark = pytest.mark.offline


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _git_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True)
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("# repo\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial commit")
    return repo


def _cfg(**overrides) -> dict:
    base = {
        "enabled": True,
        "branch_prefix": "flow/",
        "commit": True,
        "push": False,
        "pr": False,
        "protected_branches": ["main", "master"],
    }
    base.update(overrides)
    return base


RUN_ID = "20260710-153000-add-auth-3fa2b1cd"


def test_disabled_delivery_makes_zero_git_calls(tmp_path: Path):
    calls: list[list[str]] = []

    def spy(args, cwd):
        calls.append(args)
        raise AssertionError("must not be called")

    report = deliver(
        _cfg(enabled=False),
        target_repo=tmp_path,
        changed_files=["a.py"],
        run_id=RUN_ID,
        request="add auth",
        git=spy,
    )

    assert report.status == "skipped"
    assert calls == []


def test_commits_only_listed_files_on_fresh_branch(tmp_path: Path):
    repo = _git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src/feature.py").write_text("new feature\n")
    # Pre-existing dirt the flow did NOT create must stay out of the commit.
    (repo / "operator-scratch.txt").write_text("operator's own dirt\n")

    report = deliver(
        _cfg(),
        target_repo=repo,
        changed_files=["src/feature.py"],
        run_id=RUN_ID,
        request="add auth support",
    )

    assert report.status == "committed"
    assert report.branch == f"flow/{RUN_ID}"
    assert report.base_ref == "main"
    assert report.staged_files == ["src/feature.py"]
    assert report.commit_sha
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == f"flow/{RUN_ID}"
    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert committed == ["src/feature.py"]
    # The dirt is untouched and uncommitted.
    assert "operator-scratch.txt" in _git(repo, "status", "--porcelain")
    message = _git(repo, "log", "-1", "--format=%B")
    assert message.startswith(f"flow({RUN_ID}): add auth support")
    assert f"Run-Id: {RUN_ID}" in message


def test_stages_deletions(tmp_path: Path):
    repo = _git_repo(tmp_path)
    (repo / "obsolete.py").write_text("kill me\n")
    _git(repo, "add", "obsolete.py")
    _git(repo, "commit", "-m", "add obsolete file")
    (repo / "obsolete.py").unlink()

    report = deliver(
        _cfg(),
        target_repo=repo,
        changed_files=["obsolete.py"],
        run_id=RUN_ID,
        request="remove obsolete file",
    )

    assert report.status == "committed"
    assert report.staged_files == ["obsolete.py"]
    tracked = _git(repo, "ls-files").splitlines()
    assert "obsolete.py" not in tracked


def test_branch_collision_appends_suffix(tmp_path: Path):
    repo = _git_repo(tmp_path)
    _git(repo, "branch", f"flow/{RUN_ID}")
    _git(repo, "branch", f"flow/{RUN_ID}-2")
    (repo / "new.py").write_text("x\n")

    report = deliver(
        _cfg(),
        target_repo=repo,
        changed_files=["new.py"],
        run_id=RUN_ID,
        request="collide",
    )

    assert report.status == "committed"
    assert report.branch == f"flow/{RUN_ID}-3"


def test_refuses_protected_branch_name(tmp_path: Path):
    repo = _git_repo(tmp_path)
    (repo / "new.py").write_text("x\n")

    report = deliver(
        _cfg(branch_prefix="", protected_branches=["main"]),
        target_repo=repo,
        changed_files=["new.py"],
        run_id="main",
        request="sneaky",
    )

    assert report.status == "failed"
    assert "protected branch" in report.message
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_nothing_to_commit_when_files_match_tree(tmp_path: Path):
    repo = _git_repo(tmp_path)

    report = deliver(
        _cfg(),
        target_repo=repo,
        changed_files=["README.md"],  # unchanged since last commit
        run_id=RUN_ID,
        request="no-op",
    )

    assert report.status == "nothing_to_commit"


def test_nothing_to_commit_when_no_safe_files(tmp_path: Path):
    repo = _git_repo(tmp_path)

    report = deliver(
        _cfg(),
        target_repo=repo,
        changed_files=[],
        run_id=RUN_ID,
        request="empty",
    )

    assert report.status == "nothing_to_commit"
    # No branch was created for an empty delivery.
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_unsafe_paths_are_skipped_not_fatal(tmp_path: Path):
    repo = _git_repo(tmp_path)
    (repo / "good.py").write_text("good\n")

    report = deliver(
        _cfg(),
        target_repo=repo,
        changed_files=["good.py", "../evil.py", "/etc/passwd"],
        run_id=RUN_ID,
        request="mixed paths",
    )

    assert report.status == "committed"
    assert report.staged_files == ["good.py"]
    assert sorted(report.skipped_files) == ["../evil.py", "/etc/passwd"]


def test_detached_head_is_supported(tmp_path: Path):
    repo = _git_repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "--detach", sha)
    (repo / "new.py").write_text("x\n")

    report = deliver(
        _cfg(),
        target_repo=repo,
        changed_files=["new.py"],
        run_id=RUN_ID,
        request="from detached",
    )

    assert report.status == "committed"
    assert report.base_ref == "(detached)"


def test_unborn_head_creates_root_commit(tmp_path: Path):
    repo = tmp_path / "fresh"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], capture_output=True)
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "first.py").write_text("x\n")

    report = deliver(
        _cfg(),
        target_repo=repo,
        changed_files=["first.py"],
        run_id=RUN_ID,
        request="root commit",
    )

    assert report.status == "committed"
    assert report.base_ref == "(unborn)"
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == f"flow/{RUN_ID}"


def test_missing_identity_falls_back(tmp_path: Path):
    """First commit fails with git's identity error; the retry must inject a
    fallback -c user.name/-c user.email. Deterministic via a fake runner —
    real git auto-derives an identity on developer machines."""
    repo = _git_repo(tmp_path)
    (repo / "new.py").write_text("x\n")

    from crewai_headless_flow.delivery import run_git

    identity_commits: list[list[str]] = []

    def flaky_identity_git(args, cwd):
        if args and args[0] == "commit":
            return subprocess.CompletedProcess(
                args=["git", *args],
                returncode=128,
                stdout="",
                stderr="fatal: unable to auto-detect email address\n"
                "*** Please tell me who you are.",
            )
        if args and args[0] == "-c":
            identity_commits.append(args)
        return run_git(args, cwd)

    report = deliver(
        _cfg(),
        target_repo=repo,
        changed_files=["new.py"],
        run_id=RUN_ID,
        request="identity fallback",
        git=flaky_identity_git,
    )

    assert report.status == "committed"
    assert "fallback git identity" in report.message
    assert identity_commits, "retry must re-run commit with -c identity flags"
    assert any("user.name=crewai-headless-flow" in part for part in identity_commits[0])


def test_not_a_git_repo_fails_cleanly(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()

    report = deliver(
        _cfg(),
        target_repo=plain,
        changed_files=["a.py"],
        run_id=RUN_ID,
        request="no repo",
    )

    assert report.status == "failed"
    assert "not a git work tree" in report.message


def test_git_failure_via_fake_runner_never_raises(tmp_path: Path):
    def broken(args, cwd):
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=128, stdout="", stderr="catastrophe"
        )

    report = deliver(
        _cfg(),
        target_repo=tmp_path,
        changed_files=["a.py"],
        run_id=RUN_ID,
        request="broken git",
        git=broken,
    )

    assert report.status == "failed"


# --- push / pr (Phase 2, gated on verification) -----------------------------------


def _push_intercepting_git(
    pushes: list[list[str]], returncode: int = 0, stderr: str = ""
):
    """Real git for everything except `push`, which is intercepted."""

    from crewai_headless_flow.delivery import run_git

    def runner(args, cwd):
        if args and args[0] == "push":
            pushes.append(args)
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=returncode, stdout="", stderr=stderr
            )
        return run_git(args, cwd)

    return runner


def test_push_ships_branch_to_remote_without_force(tmp_path: Path):
    repo = _git_repo(tmp_path)
    (repo / "new.py").write_text("x\n")
    pushes: list[list[str]] = []

    report = deliver(
        _cfg(push=True),
        target_repo=repo,
        changed_files=["new.py"],
        run_id=RUN_ID,
        request="push me",
        git=_push_intercepting_git(pushes),
    )

    assert report.status == "committed"
    assert report.push == "pushed"
    assert report.pr == "off"
    assert pushes == [["push", "-u", "origin", f"flow/{RUN_ID}"]]
    assert not any("--force" in argv for argv in pushes)


def test_push_honors_configured_remote(tmp_path: Path):
    repo = _git_repo(tmp_path)
    (repo / "new.py").write_text("x\n")
    pushes: list[list[str]] = []

    report = deliver(
        _cfg(push=True, remote="upstream"),
        target_repo=repo,
        changed_files=["new.py"],
        run_id=RUN_ID,
        request="push me",
        git=_push_intercepting_git(pushes),
    )

    assert report.push == "pushed"
    assert pushes == [["push", "-u", "upstream", f"flow/{RUN_ID}"]]


def test_push_blocked_when_verification_not_ok(tmp_path: Path):
    repo = _git_repo(tmp_path)
    (repo / "new.py").write_text("x\n")
    pushes: list[list[str]] = []

    report = deliver(
        _cfg(push=True, pr=True),
        target_repo=repo,
        changed_files=["new.py"],
        run_id=RUN_ID,
        request="push me",
        verification_ok=False,
        git=_push_intercepting_git(pushes),
    )

    assert report.status == "committed"  # local commit still lands
    assert report.push == "blocked_unverified"
    assert report.pr == "blocked_unverified"
    assert pushes == []
    assert "blocked" in report.message


def test_push_failure_keeps_commit_and_skips_pr(tmp_path: Path):
    repo = _git_repo(tmp_path)
    (repo / "new.py").write_text("x\n")
    pushes: list[list[str]] = []

    report = deliver(
        _cfg(push=True, pr=True),
        target_repo=repo,
        changed_files=["new.py"],
        run_id=RUN_ID,
        request="push me",
        git=_push_intercepting_git(pushes, returncode=128, stderr="no remote"),
    )

    assert report.status == "committed"
    assert report.push == "failed"
    assert report.pr == "skipped_no_push"
    assert "git push failed" in report.message


def test_pr_created_via_gh_with_url_captured(tmp_path: Path):
    repo = _git_repo(tmp_path)
    (repo / "new.py").write_text("x\n")
    pushes: list[list[str]] = []
    gh_calls: list[list[str]] = []

    def fake_gh(args, cwd):
        gh_calls.append(args)
        return subprocess.CompletedProcess(
            args=["gh", *args],
            returncode=0,
            stdout="https://github.com/acme/repo/pull/42\n",
            stderr="",
        )

    report = deliver(
        _cfg(push=True, pr=True),
        target_repo=repo,
        changed_files=["new.py"],
        run_id=RUN_ID,
        request="open a pr",
        verification_note="Verification: passed (2 command(s)).",
        git=_push_intercepting_git(pushes),
        gh=fake_gh,
    )

    assert report.push == "pushed"
    assert report.pr == "created"
    assert report.pr_url == "https://github.com/acme/repo/pull/42"
    assert len(gh_calls) == 1
    argv = gh_calls[0]
    assert argv[:2] == ["pr", "create"]
    assert argv[argv.index("--head") + 1] == f"flow/{RUN_ID}"
    assert "--base" not in argv  # let gh target the repo's default branch
    body = argv[argv.index("--body") + 1]
    assert "open a pr" in body
    assert f"Run-Id: {RUN_ID}" in body
    assert "Verification: passed" in body


def test_pr_gh_failure_keeps_commit_and_push(tmp_path: Path):
    repo = _git_repo(tmp_path)
    (repo / "new.py").write_text("x\n")

    def broken_gh(args, cwd):
        raise FileNotFoundError("gh not installed")

    report = deliver(
        _cfg(push=True, pr=True),
        target_repo=repo,
        changed_files=["new.py"],
        run_id=RUN_ID,
        request="open a pr",
        git=_push_intercepting_git([]),
        gh=broken_gh,
    )

    assert report.status == "committed"
    assert report.push == "pushed"
    assert report.pr == "failed"
    assert "gh pr create failed" in report.message


def test_push_disabled_never_calls_push_or_gh(tmp_path: Path):
    repo = _git_repo(tmp_path)
    (repo / "new.py").write_text("x\n")
    pushes: list[list[str]] = []

    def forbidden_gh(args, cwd):
        raise AssertionError("gh must not be called")

    report = deliver(
        _cfg(),
        target_repo=repo,
        changed_files=["new.py"],
        run_id=RUN_ID,
        request="local only",
        git=_push_intercepting_git(pushes),
        gh=forbidden_gh,
    )

    assert report.push == "off"
    assert report.pr == "off"
    assert pushes == []


def test_legacy_requested_not_implemented_reports_still_deserialize():
    """Phase-1 state.json files carry the retired flag value; resume must
    not choke on them."""
    revived = DeliveryReport.model_validate(
        {
            "status": "committed",
            "push": "requested_not_implemented",
            "pr": "requested_not_implemented",
        }
    )

    assert revived.push == "requested_not_implemented"


def test_delivery_report_round_trips_through_flow_state():
    report = DeliveryReport(
        status="committed",
        branch=f"flow/{RUN_ID}",
        commit_sha="abc123",
        staged_files=["a.py"],
    )
    state = FlowState(request="r", target_repo="/tmp/x", delivery_report=report)

    revived = FlowState.model_validate(state.model_dump())

    assert revived.delivery_report is not None
    assert revived.delivery_report.status == "committed"
    assert revived.delivery_report.branch == f"flow/{RUN_ID}"


# --- flow integration ------------------------------------------------------------


class FinalizeWritingWorker:
    """Simulates finalize writing an ADR without self-reporting it."""

    def run(self, **kwargs) -> CoderResult:
        cwd = Path(kwargs["cwd"])
        (cwd / "docs").mkdir(exist_ok=True)
        (cwd / "docs/ADR.md").write_text("# ADR\n")
        return CoderResult(
            summary="wrote ADR",
            changed_files=[],  # deliberately under-reported (grok behavior)
            raw_output="wrote ADR",
            exit_code=0,
        )


def test_finalize_delivers_committed_branch_including_adr(tmp_path: Path):
    repo = _git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src/feature.py").write_text("feature\n")

    cfg = FlowConfig(
        skills={"finalize": "documentation-and-adrs"},
        workers={"finalize": {"worker": "claude"}},
        defaults={"worker": "codex", "timeout": 300},
        deliver={"enabled": True},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="ship the feature",
        target_repo=str(repo),
        run_id=RUN_ID,
        changed_files=["src/feature.py"],
        review_status="pass",
    )
    flow._workers["finalize"] = FinalizeWritingWorker()  # type: ignore

    cast(Any, flow).finalize("pass")

    assert flow.state.status == "completed"
    report = flow.state.delivery_report
    assert report is not None
    assert report.status == "committed"
    assert report.branch == f"flow/{RUN_ID}"
    # The snapshot diff caught the ADR the worker didn't self-report.
    assert "docs/ADR.md" in report.staged_files
    assert "src/feature.py" in report.staged_files
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == f"flow/{RUN_ID}"


def test_finalize_delivery_failure_keeps_run_completed(tmp_path: Path):
    plain_repo = tmp_path / "plain"
    plain_repo.mkdir()

    cfg = FlowConfig(
        skills={"finalize": "documentation-and-adrs"},
        workers={"finalize": {"worker": "claude"}},
        defaults={"worker": "codex", "timeout": 300},
        deliver={"enabled": True},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="ship it",
        target_repo=str(plain_repo),
        run_id=RUN_ID,
        changed_files=["a.py"],
        review_status="pass",
    )
    flow._workers["finalize"] = FinalizeWritingWorker()  # type: ignore

    cast(Any, flow).finalize("pass")

    assert flow.state.status == "completed"
    assert flow.state.delivery_report is not None
    assert flow.state.delivery_report.status == "failed"
    assert any("Delivery failed" in err for err in flow.state.errors)


def test_finalize_without_deliver_enabled_records_no_report(tmp_path: Path):
    repo = _git_repo(tmp_path)

    cfg = FlowConfig(
        skills={"finalize": "documentation-and-adrs"},
        workers={"finalize": {"worker": "claude"}},
        defaults={"worker": "codex", "timeout": 300},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="default off",
        target_repo=str(repo),
        review_status="pass",
    )
    flow._workers["finalize"] = FinalizeWritingWorker()  # type: ignore

    cast(Any, flow).finalize("pass")

    assert flow.state.status == "completed"
    assert flow.state.delivery_report is None
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


# --- verification predicate at the flow seam ---------------------------------------


def _predicate_flow(tmp_path: Path, verify_commands: list[str]):
    repo = _git_repo(tmp_path)
    cfg = FlowConfig(
        skills={"finalize": "documentation-and-adrs"},
        workers={"finalize": {"worker": "claude"}},
        defaults={"worker": "codex", "timeout": 300},
        deliver={"enabled": True, "push": True},
        verify={"commands": verify_commands},
    )
    flow = CrewAIHeadlessFlow(config=cfg)
    flow._state = FlowState(  # type: ignore[attr-defined]
        request="ship it",
        target_repo=str(repo),
        run_id=RUN_ID,
        changed_files=["src/feature.py"],
        review_status="pass",
    )
    flow._workers["finalize"] = FinalizeWritingWorker()  # type: ignore
    return flow


def _spy_deliver(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def spy(cfg, **kwargs):
        calls.append(kwargs)
        return DeliveryReport(status="committed", branch="flow/x")

    monkeypatch.setattr("crewai_headless_flow.flow.deliver", spy)
    return calls


def test_flow_blocks_push_when_verify_configured_but_never_ran(
    tmp_path: Path, monkeypatch
):
    from crewai_headless_flow.verification import VerificationReport

    flow = _predicate_flow(tmp_path, ["uv run pytest -q"])
    calls = _spy_deliver(monkeypatch)

    cast(Any, flow).finalize("pass")

    assert calls[0]["verification_ok"] is False

    # ...and when the latest round failed.
    (tmp_path / "second").mkdir()
    flow = _predicate_flow(tmp_path / "second", ["uv run pytest -q"])
    flow.state.verification_runs.append(VerificationReport(passed=False, mode="gate"))
    cast(Any, flow).finalize("pass")
    assert calls[1]["verification_ok"] is False


def test_flow_allows_push_when_latest_verification_passed(tmp_path: Path, monkeypatch):
    from crewai_headless_flow.verification import VerificationReport

    flow = _predicate_flow(tmp_path, ["uv run pytest -q"])
    flow.state.verification_runs.append(VerificationReport(passed=False, mode="gate"))
    flow.state.verification_runs.append(VerificationReport(passed=True, mode="gate"))
    calls = _spy_deliver(monkeypatch)

    cast(Any, flow).finalize("pass")

    assert calls[0]["verification_ok"] is True
    assert "Verification: passed" in calls[0]["verification_note"]


def test_flow_allows_push_when_verify_unconfigured(tmp_path: Path, monkeypatch):
    flow = _predicate_flow(tmp_path, [])
    calls = _spy_deliver(monkeypatch)

    cast(Any, flow).finalize("pass")

    assert calls[0]["verification_ok"] is True
    assert "not configured" in calls[0]["verification_note"]


def test_flow_records_push_failure_in_errors(tmp_path: Path, monkeypatch):
    flow = _predicate_flow(tmp_path, [])

    def failing_push_deliver(cfg, **kwargs):
        return DeliveryReport(
            status="committed",
            branch="flow/x",
            push="failed",
            message="git push failed: no remote",
        )

    monkeypatch.setattr("crewai_headless_flow.flow.deliver", failing_push_deliver)

    cast(Any, flow).finalize("pass")

    assert flow.state.status == "completed"
    assert any("Delivery push failed" in err for err in flow.state.errors)
