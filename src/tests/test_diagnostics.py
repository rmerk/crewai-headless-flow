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
    assert report.tooling["pyproject.toml"] is True
    assert report.tooling["README.md"] is True
    assert json.loads(json.dumps(report.to_dict())) == report.to_dict()
