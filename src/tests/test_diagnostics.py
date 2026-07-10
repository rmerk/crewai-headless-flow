from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml


pytestmark = pytest.mark.offline


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    (d / "skills.yaml").write_text(
        yaml.safe_dump(
            {
                "stages": {
                    "plan": "planning-and-task-breakdown",
                    "do_work": "incremental-implementation",
                    "review": "code-review-and-quality",
                    "finalize": "documentation-and-adrs",
                }
            }
        )
    )
    (d / "worker.yaml").write_text(
        yaml.safe_dump(
            {
                "defaults": {"worker": "codex", "model": None, "timeout": 300},
                "stages": {},
                "human_feedback": {"enabled": False},
            }
        )
    )
    return d


def test_doctor_fails_missing_yaml_file(config_dir: Path, monkeypatch):
    from crewai_headless_flow.diagnostics import run_doctor

    (config_dir / "worker.yaml").unlink()
    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which", lambda name: None
    )

    report = run_doctor(config_dir=config_dir)

    assert report.status == "fail"
    assert any("worker.yaml" in failure for failure in report.failures)


def test_doctor_fails_malformed_yaml_type(config_dir: Path, monkeypatch):
    from crewai_headless_flow.diagnostics import run_doctor

    (config_dir / "skills.yaml").write_text("[]")
    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which", lambda name: None
    )

    report = run_doctor(config_dir=config_dir)

    assert report.status == "fail"
    assert any(
        "skills.yaml must contain a mapping" in failure for failure in report.failures
    )


def test_doctor_fails_missing_required_skill_stage(config_dir: Path, monkeypatch):
    from crewai_headless_flow.diagnostics import run_doctor

    data = yaml.safe_load((config_dir / "skills.yaml").read_text())
    del data["stages"]["review"]
    (config_dir / "skills.yaml").write_text(yaml.safe_dump(data))
    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which", lambda name: None
    )

    report = run_doctor(config_dir=config_dir)

    assert report.status == "fail"
    assert any(
        "missing required stages: review" in failure for failure in report.failures
    )


def test_doctor_fails_unsupported_worker(config_dir: Path, monkeypatch):
    from crewai_headless_flow.diagnostics import run_doctor

    data = yaml.safe_load((config_dir / "worker.yaml").read_text())
    data["defaults"]["worker"] = "bad-worker"
    (config_dir / "worker.yaml").write_text(yaml.safe_dump(data))
    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which", lambda name: None
    )

    report = run_doctor(config_dir=config_dir)

    assert report.status == "fail"
    assert any("Unsupported worker" in failure for failure in report.failures)


def test_doctor_fails_missing_referenced_skill(
    config_dir: Path, tmp_path: Path, monkeypatch
):
    from crewai_headless_flow.diagnostics import run_doctor

    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which", lambda name: None
    )

    report = run_doctor(config_dir=config_dir, skills_root=tmp_path / "missing-skills")

    assert report.status == "fail"
    assert any("Skill not found" in failure for failure in report.failures)


def test_doctor_fails_missing_configured_cli(config_dir: Path, monkeypatch):
    from crewai_headless_flow.diagnostics import run_doctor

    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which",
        lambda name: "/bin/ollama" if name == "ollama" else None,
    )
    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics._run_probe",
        lambda cmd, timeout=3: __import__(
            "crewai_headless_flow.diagnostics"
        ).diagnostics.ProbeResult(returncode=0, stdout="", stderr=""),
    )

    report = run_doctor(config_dir=config_dir)

    assert report.status == "fail"
    assert any("CLI not found: codex" in failure for failure in report.failures)


def test_doctor_fails_missing_required_cli_flag(config_dir: Path, monkeypatch):
    from crewai_headless_flow.diagnostics import ProbeResult, run_doctor

    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which", lambda name: f"/bin/{name}"
    )
    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics._run_probe",
        lambda cmd, timeout=3: ProbeResult(
            returncode=0, stdout="codex help", stderr=""
        ),
    )

    report = run_doctor(config_dir=config_dir)

    assert report.status == "fail"
    assert any("missing required flags" in failure for failure in report.failures)


def test_doctor_fails_ollama_unavailable(config_dir: Path, monkeypatch):
    from crewai_headless_flow.diagnostics import ProbeResult, run_doctor

    worker_data = yaml.safe_load((config_dir / "worker.yaml").read_text())
    worker_data["stages"]["plan"] = {
        "worker": "codex",
        "crew": {"enabled": True, "process": "sequential"},
    }
    (config_dir / "worker.yaml").write_text(yaml.safe_dump(worker_data))

    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which", lambda name: f"/bin/{name}"
    )

    def fake_probe(cmd, timeout=3):
        if cmd[0] == "ollama":
            return ProbeResult(returncode=1, stdout="", stderr="connection refused")
        return ProbeResult(
            returncode=0,
            stdout="--sandbox --output-schema --always-approve --json-schema",
            stderr="",
        )

    monkeypatch.setattr("crewai_headless_flow.diagnostics._run_probe", fake_probe)

    report = run_doctor(config_dir=config_dir)

    assert report.status == "fail"
    assert any("ollama list failed" in failure for failure in report.failures)


def test_doctor_does_not_require_ollama_when_no_crews_enabled(
    config_dir: Path, monkeypatch
):
    from crewai_headless_flow.diagnostics import ProbeResult, run_doctor

    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which",
        lambda name: None if name == "ollama" else f"/bin/{name}",
    )
    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics._run_probe",
        lambda cmd, timeout=3: ProbeResult(
            returncode=0,
            stdout="--sandbox --output-schema --always-approve --json-schema",
            stderr="",
        ),
    )

    report = run_doctor(config_dir=config_dir)

    assert not any("ollama" in failure for failure in report.failures)


def test_doctor_warns_when_do_work_crew_is_enabled(config_dir: Path, monkeypatch):
    from crewai_headless_flow.diagnostics import ProbeResult, run_doctor

    worker_data = yaml.safe_load((config_dir / "worker.yaml").read_text())
    worker_data["stages"]["do_work"] = {
        "worker": "codex",
        "crew": {"enabled": True, "process": "sequential"},
    }
    (config_dir / "worker.yaml").write_text(yaml.safe_dump(worker_data))

    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which", lambda name: f"/bin/{name}"
    )

    def fake_probe(cmd, timeout=3):
        if cmd[0] == "ollama":
            return ProbeResult(returncode=0, stdout="llama3.2", stderr="")
        return ProbeResult(
            returncode=0,
            stdout="--sandbox --output-schema --always-approve --json-schema",
            stderr="",
        )

    monkeypatch.setattr("crewai_headless_flow.diagnostics._run_probe", fake_probe)

    report = run_doctor(config_dir=config_dir)

    assert any(check.name == "config.do_work_crew" for check in report.checks)
    assert any(check.name == "cli.ollama" for check in report.checks)


def test_doctor_skips_ollama_for_custom_crew_provider(config_dir: Path, monkeypatch):
    from crewai_headless_flow.diagnostics import ProbeResult, run_doctor

    worker_data = yaml.safe_load((config_dir / "worker.yaml").read_text())
    worker_data["stages"]["review"] = {
        "worker": "codex",
        "crew": {
            "enabled": True,
            "process": "sequential",
            "llm": {
                "model": "gpt-4o-mini",
                "base_url": "https://api.openai.com/v1",
            },
        },
    }
    (config_dir / "worker.yaml").write_text(yaml.safe_dump(worker_data))

    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which",
        lambda name: None if name == "ollama" else f"/bin/{name}",
    )
    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics._run_probe",
        lambda cmd, timeout=3: ProbeResult(
            returncode=0,
            stdout="--sandbox --output-schema --always-approve --json-schema",
            stderr="",
        ),
    )

    report = run_doctor(config_dir=config_dir)

    assert report.status == "warn"
    review_check = next(
        check for check in report.checks if check.name == "config.review_crew"
    )
    assert review_check.details["ollama_required"] is False
    assert "external/custom" in review_check.message
    assert not any(check.name == "cli.ollama" for check in report.checks)


def test_doctor_accepts_gemini_worker(config_dir: Path, monkeypatch):
    from crewai_headless_flow.diagnostics import ProbeResult, run_doctor

    worker_data = yaml.safe_load((config_dir / "worker.yaml").read_text())
    worker_data["stages"]["finalize"] = {"worker": "gemini"}
    (config_dir / "worker.yaml").write_text(yaml.safe_dump(worker_data))

    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which", lambda name: f"/bin/{name}"
    )
    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics._run_probe",
        lambda cmd, timeout=3: ProbeResult(
            returncode=0,
            stdout="--sandbox --output-schema --prompt --approval-mode --output-format",
            stderr="",
        ),
    )

    report = run_doctor(config_dir=config_dir)

    assert report.status == "pass"
    assert any(check.name == "cli.gemini" for check in report.checks)


def test_doctor_accepts_cursor_worker(config_dir: Path, monkeypatch):
    from crewai_headless_flow.diagnostics import ProbeResult, run_doctor

    worker_data = yaml.safe_load((config_dir / "worker.yaml").read_text())
    worker_data["stages"]["finalize"] = {"worker": "cursor"}
    (config_dir / "worker.yaml").write_text(yaml.safe_dump(worker_data))

    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which", lambda name: f"/bin/{name}"
    )
    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics._run_probe",
        lambda cmd, timeout=3: ProbeResult(
            returncode=0,
            stdout=(
                "--sandbox --output-schema --always-approve --output-format "
                "--permission-mode --json-schema --prompt --approval-mode "
                "--print --output-format --plan --force --trust --workspace --model"
            ),
            stderr="",
        ),
    )
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)

    report = run_doctor(config_dir=config_dir)

    assert report.status == "warn"
    assert any(check.name == "cli.cursor" for check in report.checks)
    auth_check = next(
        check for check in report.checks if check.name == "auth.cursor_api_key"
    )
    assert auth_check.status == "warn"


def test_doctor_cursor_auth_passes_when_api_key_set(config_dir: Path, monkeypatch):
    from crewai_headless_flow.diagnostics import ProbeResult, run_doctor

    worker_data = yaml.safe_load((config_dir / "worker.yaml").read_text())
    worker_data["stages"]["finalize"] = {"worker": "cursor"}
    (config_dir / "worker.yaml").write_text(yaml.safe_dump(worker_data))

    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which", lambda name: f"/bin/{name}"
    )
    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics._run_probe",
        lambda cmd, timeout=3: ProbeResult(
            returncode=0,
            stdout=(
                "--sandbox --output-schema --always-approve --output-format "
                "--permission-mode --json-schema --prompt --approval-mode "
                "--print --output-format --plan --force --trust --workspace --model"
            ),
            stderr="",
        ),
    )
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")

    report = run_doctor(config_dir=config_dir)

    auth_check = next(
        check for check in report.checks if check.name == "auth.cursor_api_key"
    )
    assert auth_check.status == "pass"


def test_doctor_includes_resolved_runtime_metadata(config_dir: Path, monkeypatch):
    from crewai_headless_flow.diagnostics import ProbeResult, run_doctor

    worker_data = yaml.safe_load((config_dir / "worker.yaml").read_text())
    worker_data["stages"] = {
        "do_work": {
            "worker": "grok",
            "always_approve": True,
            "parallel": {"enabled": True, "max_workers": 4},
        },
        "review": {
            "worker": "codex",
            "sandbox": "read-only",
            "crew": {
                "enabled": True,
                "process": "sequential",
                "llm": {
                    "model": "gpt-4o-mini",
                    "base_url": "https://api.openai.com/v1",
                },
            },
        },
    }
    (config_dir / "worker.yaml").write_text(yaml.safe_dump(worker_data))

    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which",
        lambda name: None if name == "ollama" else f"/bin/{name}",
    )
    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics._run_probe",
        lambda cmd, timeout=3: ProbeResult(
            returncode=0,
            stdout="--sandbox --output-schema --always-approve --output-format --json-schema",
            stderr="",
        ),
    )

    report = run_doctor(config_dir=config_dir)
    data = report.to_dict()
    resolved_stages = {
        stage["stage"]: stage for stage in data["resolved_runtime"]["stages"]
    }

    assert report.status == "warn"
    assert data["resolved_runtime"]["human_feedback"]["enabled"] is False
    assert data["resolved_runtime"]["human_feedback"]["before_do_work"] is True
    assert data["resolved_runtime"]["human_feedback"]["before_finalize"] is True
    assert resolved_stages["do_work"]["runtime_knobs"] == {
        "parallel": {"enabled": True, "max_workers": 4}
    }
    assert resolved_stages["do_work"]["enforced_declarations"] == {
        "always_approve": True
    }
    assert resolved_stages["do_work"]["can_mutate"] is True
    assert resolved_stages["review"]["runtime_knobs"] == {
        "crew": {
            "enabled": True,
            "llm": {
                "model": "gpt-4o-mini",
                "base_url": "https://api.openai.com/v1",
            },
        }
    }
    assert resolved_stages["review"]["enforced_declarations"] == {
        "sandbox": "read-only",
        "crew": {"process": "sequential"},
    }
    assert resolved_stages["review"]["notes"] == ["crew_llm_provider=external/custom"]
    assert resolved_stages["review"]["can_mutate"] is False


def test_preflight_fails_missing_target_path(tmp_path: Path):
    from crewai_headless_flow.diagnostics import run_preflight

    report = run_preflight(tmp_path / "missing")

    assert report.status == "fail"
    assert any("does not exist" in failure for failure in report.failures)


def test_preflight_fails_file_target(tmp_path: Path):
    from crewai_headless_flow.diagnostics import run_preflight

    target = tmp_path / "file.txt"
    target.write_text("not a directory")

    report = run_preflight(target)

    assert report.status == "fail"
    assert any("not a directory" in failure for failure in report.failures)


def test_preflight_warns_non_git_directory(tmp_path: Path, monkeypatch):
    from crewai_headless_flow.diagnostics import run_preflight

    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which", lambda name: "/bin/git"
    )
    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics._run_probe",
        lambda cmd, timeout=3: __import__(
            "crewai_headless_flow.diagnostics"
        ).diagnostics.ProbeResult(
            returncode=128, stdout="", stderr="not a git repository"
        ),
    )

    report = run_preflight(tmp_path)

    assert report.status == "warn"
    assert "non-git directory" in " ".join(report.warnings)


def test_preflight_fails_merge_conflict(tmp_path: Path, monkeypatch):
    from crewai_headless_flow.diagnostics import ProbeResult, run_preflight

    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which", lambda name: "/bin/git"
    )

    def fake_probe(cmd, timeout=3):
        if "rev-parse" in cmd:
            if "--is-inside-work-tree" in cmd:
                return ProbeResult(returncode=0, stdout="true\n", stderr="")
            if "--abbrev-ref" in cmd:
                return ProbeResult(returncode=0, stdout="main\n", stderr="")
        if "status" in cmd:
            return ProbeResult(returncode=0, stdout="UU src/example.py\n", stderr="")
        return ProbeResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("crewai_headless_flow.diagnostics._run_probe", fake_probe)

    report = run_preflight(tmp_path)

    assert report.status == "fail"
    assert any("merge conflicts" in failure for failure in report.failures)
    assert report.git["has_conflicts"] is True


def test_preflight_reports_clean_git_repo_and_tooling(tmp_path: Path, monkeypatch):
    from crewai_headless_flow.diagnostics import ProbeResult, run_preflight

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "README.md").write_text("# x\n")
    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which", lambda name: "/bin/git"
    )

    def fake_probe(cmd, timeout=3):
        if "rev-parse" in cmd:
            if "--is-inside-work-tree" in cmd:
                return ProbeResult(returncode=0, stdout="true\n", stderr="")
            if "--abbrev-ref" in cmd:
                return ProbeResult(returncode=0, stdout="main\n", stderr="")
        if "status" in cmd:
            return ProbeResult(returncode=0, stdout="", stderr="")
        return ProbeResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("crewai_headless_flow.diagnostics._run_probe", fake_probe)

    report = run_preflight(tmp_path)

    assert report.status == "pass"
    assert report.git["is_git_repo"] is True
    assert report.git["branch"] == "main"
    assert report.tooling["pyproject.toml"] is True
    assert report.tooling["README.md"] is True
    assert json.loads(json.dumps(report.to_dict())) == report.to_dict()


def _codex_probe(cmd, timeout=3):
    from crewai_headless_flow.diagnostics import ProbeResult

    return ProbeResult(
        returncode=0,
        stdout="--sandbox --output-schema --always-approve --json-schema",
        stderr="",
    )


def _run_doctor_with_hf(config_dir: Path, monkeypatch, human_feedback: dict):
    from crewai_headless_flow.diagnostics import run_doctor

    worker_data = yaml.safe_load((config_dir / "worker.yaml").read_text())
    worker_data["human_feedback"] = human_feedback
    (config_dir / "worker.yaml").write_text(yaml.safe_dump(worker_data))
    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which", lambda name: f"/bin/{name}"
    )
    monkeypatch.setattr("crewai_headless_flow.diagnostics._run_probe", _codex_probe)
    return run_doctor(config_dir=config_dir)


def _conditional_check(report):
    return next(
        (c for c in report.checks if c.name == "config.human_feedback.conditional"),
        None,
    )


def test_doctor_warns_conditional_mode_with_no_triggers(config_dir: Path, monkeypatch):
    report = _run_doctor_with_hf(
        config_dir, monkeypatch, {"enabled": True, "mode": "conditional"}
    )

    check = _conditional_check(report)
    assert check is not None
    assert check.status == "warn"
    assert "no triggers are enabled" in check.message


def test_doctor_warns_conditional_mode_with_dead_gate_boolean(
    config_dir: Path, monkeypatch
):
    report = _run_doctor_with_hf(
        config_dir,
        monkeypatch,
        {
            "enabled": True,
            "mode": "conditional",
            "before_finalize": True,
            "conditional": {"triggers": {"repeated_task_failure": {"enabled": True}}},
        },
    )

    check = _conditional_check(report)
    assert check is not None
    assert check.status == "warn"
    assert "before_finalize" in check.message


def test_doctor_passes_conditional_mode_with_enabled_trigger(
    config_dir: Path, monkeypatch
):
    report = _run_doctor_with_hf(
        config_dir,
        monkeypatch,
        {
            "enabled": True,
            "mode": "conditional",
            # Zero the trigger-less gates so their defaults (before_finalize
            # defaults True) are not flagged as dead config.
            "before_do_work": False,
            "before_finalize": False,
            "conditional": {
                "triggers": {"approaching_max_revisions": {"enabled": True}}
            },
        },
    )

    check = _conditional_check(report)
    assert check is not None
    assert check.status == "pass"
    assert "approaching_max_revisions" in check.message


def test_doctor_adds_no_conditional_check_in_static_mode(config_dir: Path, monkeypatch):
    report = _run_doctor_with_hf(
        config_dir, monkeypatch, {"enabled": True, "mode": "static"}
    )

    assert _conditional_check(report) is None


# =============================================================================
# config.verify doctor checks (autonomy Gap 1)
# =============================================================================


def _run_doctor_with_blocks(config_dir: Path, monkeypatch, **blocks):
    from crewai_headless_flow.diagnostics import run_doctor

    worker_data = yaml.safe_load((config_dir / "worker.yaml").read_text())
    worker_data.update(blocks)
    (config_dir / "worker.yaml").write_text(yaml.safe_dump(worker_data))
    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which", lambda name: f"/bin/{name}"
    )
    monkeypatch.setattr("crewai_headless_flow.diagnostics._run_probe", _codex_probe)
    return run_doctor(config_dir=config_dir)


def _verify_check(report):
    return next((c for c in report.checks if c.name == "config.verify"), None)


def test_doctor_reports_verify_configuration(config_dir: Path, monkeypatch):
    report = _run_doctor_with_blocks(
        config_dir,
        monkeypatch,
        verify={"commands": ["uv run pytest -q", "uv run ruff check ."]},
    )

    check = _verify_check(report)
    assert check is not None
    assert check.status == "pass"
    assert "mode: gate" in check.message
    assert "2 command(s)" in check.message


def test_doctor_warns_when_push_enabled_without_verify_commands(
    config_dir: Path, monkeypatch
):
    report = _run_doctor_with_blocks(
        config_dir,
        monkeypatch,
        deliver={"enabled": True, "push": True},
    )

    check = _verify_check(report)
    assert check is not None
    assert check.status == "warn"
    assert "unverified" in check.message


def test_doctor_passes_verify_unconfigured_without_shipping(
    config_dir: Path, monkeypatch
):
    report = _run_doctor_with_blocks(config_dir, monkeypatch)

    check = _verify_check(report)
    assert check is not None
    assert check.status == "pass"
    assert "No verification commands" in check.message


def test_doctor_warns_when_pr_enabled_without_gh(config_dir: Path, monkeypatch):
    report = _run_doctor_with_blocks(
        config_dir,
        monkeypatch,
        deliver={"enabled": True, "push": True, "pr": True},
    )
    # _run_doctor_with_blocks fakes shutil.which to find everything; re-run
    # with gh explicitly missing.
    from crewai_headless_flow.diagnostics import run_doctor

    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which",
        lambda name: None if name == "gh" else f"/bin/{name}",
    )
    report = run_doctor(config_dir=config_dir)

    check = next((c for c in report.checks if c.name == "cli.gh"), None)
    assert check is not None
    assert check.status == "warn"
    assert "gh" in check.message


def test_doctor_passes_gh_check_when_present(config_dir: Path, monkeypatch):
    report = _run_doctor_with_blocks(
        config_dir,
        monkeypatch,
        deliver={"enabled": True, "push": True, "pr": True},
    )

    check = next((c for c in report.checks if c.name == "cli.gh"), None)
    assert check is not None
    assert check.status == "pass"


def test_doctor_adds_no_gh_check_when_pr_disabled(config_dir: Path, monkeypatch):
    report = _run_doctor_with_blocks(config_dir, monkeypatch)

    assert not any(c.name == "cli.gh" for c in report.checks)


def test_doctor_warns_deny_paths_with_serial_in_place(config_dir: Path, monkeypatch):
    report = _run_doctor_with_blocks(
        config_dir,
        monkeypatch,
        paths={"deny": ["*.env"]},
    )

    check = next((c for c in report.checks if c.name == "config.paths"), None)
    assert check is not None
    assert check.status == "warn"
    assert "post-hoc" in check.message


def test_doctor_passes_deny_paths_with_isolation_copy(config_dir: Path, monkeypatch):
    worker_data = yaml.safe_load((config_dir / "worker.yaml").read_text())
    stages = worker_data.setdefault("stages", {})
    stages.setdefault("do_work", {})["isolation"] = "copy"
    (config_dir / "worker.yaml").write_text(yaml.safe_dump(worker_data))

    report = _run_doctor_with_blocks(
        config_dir,
        monkeypatch,
        paths={"deny": ["*.env"]},
    )

    check = next((c for c in report.checks if c.name == "config.paths"), None)
    assert check is not None
    assert check.status == "pass"


def test_doctor_adds_no_paths_check_when_deny_empty(config_dir: Path, monkeypatch):
    report = _run_doctor_with_blocks(config_dir, monkeypatch)

    assert not any(c.name == "config.paths" for c in report.checks)


# =============================================================================
# WorkerSpec derivations + configured-binary probing (autonomy Gap 10)
# =============================================================================


def test_worker_dicts_are_derived_from_worker_specs():
    from crewai_headless_flow.diagnostics import (
        SUPPORTED_WORKERS,
        WORKER_BINARIES,
        WORKER_HELP_COMMANDS,
        WORKER_REQUIRED_FLAGS,
    )
    from crewai_headless_flow.flow import WORKER_ADAPTERS
    from crewai_headless_flow.workers import WORKER_SPECS

    assert set(WORKER_ADAPTERS) == set(WORKER_SPECS)
    assert SUPPORTED_WORKERS == set(WORKER_SPECS)
    assert set(WORKER_BINARIES) == set(WORKER_SPECS)
    assert set(WORKER_HELP_COMMANDS) == set(WORKER_SPECS)
    assert set(WORKER_REQUIRED_FLAGS) == set(WORKER_SPECS)
    for name, spec in WORKER_SPECS.items():
        assert WORKER_ADAPTERS[name] is spec.adapter_cls
        assert WORKER_BINARIES[name] == spec.binary
        assert WORKER_HELP_COMMANDS[name] == spec.help_command
        assert WORKER_REQUIRED_FLAGS[name] == spec.required_flags
        assert spec.help_command[0] == spec.binary


def test_doctor_probes_configured_worker_binary(config_dir: Path, monkeypatch):
    from crewai_headless_flow.diagnostics import run_doctor

    worker_data = yaml.safe_load((config_dir / "worker.yaml").read_text())
    worker_data["workers"] = {"codex": {"binary": "/opt/bin/codex-nightly"}}
    (config_dir / "worker.yaml").write_text(yaml.safe_dump(worker_data))

    which_calls: list[str] = []

    def fake_which(name: str):
        which_calls.append(name)
        return f"/bin/{name}"

    probe_calls: list[tuple[str, ...]] = []

    def fake_probe(command):
        probe_calls.append(tuple(command))
        return _codex_probe(command)

    monkeypatch.setattr("crewai_headless_flow.diagnostics.shutil.which", fake_which)
    monkeypatch.setattr("crewai_headless_flow.diagnostics._run_probe", fake_probe)

    run_doctor(config_dir=config_dir)

    assert "/opt/bin/codex-nightly" in which_calls
    assert ("/opt/bin/codex-nightly", "exec", "--help") in probe_calls
    assert ("/opt/bin/codex-nightly", "--version") in probe_calls
