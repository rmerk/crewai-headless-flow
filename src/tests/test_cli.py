from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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


def test_legacy_run_invocation_routes_to_flow_backend(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    from crewai_headless_flow import cli

    calls: list[dict] = []

    def fake_run_headless_flow(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status="completed", model_dump=lambda: {"status": "completed"}
        )

    monkeypatch.setattr(cli, "run_headless_flow", fake_run_headless_flow)

    rc = cli.main(
        [
            "--request",
            "add tests",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
        ]
    )

    assert rc == 0
    assert calls == [
        {
            "request": "add tests",
            "target_repo": str(tmp_path.resolve()),
            "max_revisions": 2,
            "config": calls[0]["config"],
        }
    ]


def test_legacy_request_value_can_equal_subcommand_name(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    from crewai_headless_flow import cli

    calls: list[dict] = []

    def fake_run_headless_flow(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status="completed", model_dump=lambda: {"status": "completed"}
        )

    monkeypatch.setattr(cli, "run_headless_flow", fake_run_headless_flow)

    rc = cli.main(
        [
            "--request",
            "run",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
        ]
    )

    assert rc == 0
    assert calls[0]["request"] == "run"


def test_explicit_run_invocation_routes_config_and_max_revisions(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    from crewai_headless_flow import cli

    calls: list[dict] = []

    def fake_run_headless_flow(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status="completed", model_dump=lambda: {"status": "completed"}
        )

    monkeypatch.setattr(cli, "run_headless_flow", fake_run_headless_flow)

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--max-revisions",
            "4",
            "--config-dir",
            str(config_dir),
        ]
    )

    assert rc == 0
    assert calls[0]["request"] == "ship cli"
    assert calls[0]["target_repo"] == str(tmp_path.resolve())
    assert calls[0]["max_revisions"] == 4


def test_mixed_legacy_args_and_subcommand_are_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    from crewai_headless_flow import cli

    rc = cli.main(
        [
            "--request",
            "ambiguous",
            "--target-repo",
            str(tmp_path),
            "run",
        ]
    )

    err = capsys.readouterr().err
    assert rc == 2
    assert "legacy --request/--target-repo cannot be combined" in err


def test_run_exception_prints_deterministic_error(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    from crewai_headless_flow import cli

    def fake_run_headless_flow(**kwargs):
        raise RuntimeError("worker exploded")

    monkeypatch.setattr(cli, "run_headless_flow", fake_run_headless_flow)

    rc = cli.main(
        [
            "run",
            "--request",
            "fail",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "Error: worker exploded" in err
    assert "Traceback" not in err


def test_run_fails_preflight_before_flow_backend(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    from crewai_headless_flow import cli

    def fail_if_run_headless_flow(**kwargs):
        raise AssertionError("run must stop before constructing the Flow")

    monkeypatch.setattr(cli, "run_headless_flow", fail_if_run_headless_flow)

    rc = cli.main(
        [
            "run",
            "--request",
            "should stop",
            "--target-repo",
            str(tmp_path / "missing"),
            "--config-dir",
            str(config_dir),
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "Preflight failed" in err
    assert "does not exist" in err


def test_json_doctor_output_is_parseable_without_flow_construction(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    from crewai_headless_flow import cli

    def fail_if_run_headless_flow(**kwargs):
        raise AssertionError("doctor must not run the Flow backend")

    monkeypatch.setattr(cli, "run_headless_flow", fail_if_run_headless_flow)
    monkeypatch.setattr(cli.diagnostics.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        cli.diagnostics,
        "_run_probe",
        lambda cmd, timeout=3: cli.diagnostics.ProbeResult(
            returncode=0,
            stdout=" ".join(cmd)
            + " --sandbox --output-schema --always-approve --json-schema",
            stderr="",
        ),
    )

    rc = cli.main(["doctor", "--config-dir", str(config_dir), "--format", "json"])

    out = capsys.readouterr().out
    data = json.loads(out)
    assert rc in {0, 1}
    assert data["status"] in {"pass", "warn", "fail"}
    assert isinstance(data["checks"], list)
    assert json.loads(json.dumps(data)) == data


def test_doctor_checks_each_configured_worker_cli(
    config_dir: Path, monkeypatch, capsys
):
    from crewai_headless_flow import cli

    worker_file = config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"] = {
        "plan": {"worker": "codex"},
        "do_work": {"worker": "grok"},
        "review": {"worker": "claude"},
        "finalize": {"worker": "codex"},
    }
    worker_file.write_text(yaml.safe_dump(data))

    monkeypatch.setattr(cli.diagnostics.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        cli.diagnostics,
        "_run_probe",
        lambda cmd, timeout=3: cli.diagnostics.ProbeResult(
            returncode=0,
            stdout=" ".join(cmd)
            + " --sandbox --output-schema --always-approve --output-format --permission-mode --json-schema",
            stderr="",
        ),
    )

    rc = cli.main(["doctor", "--config-dir", str(config_dir), "--format", "json"])

    data = json.loads(capsys.readouterr().out)
    check_names = {check["name"] for check in data["checks"]}
    assert rc == 0
    assert {"cli.codex", "cli.grok", "cli.claude"}.issubset(check_names)


def test_preflight_json_includes_canonical_config_dir(
    tmp_path: Path, config_dir: Path, capsys
):
    from crewai_headless_flow import cli

    rc = cli.main(
        [
            "preflight",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--format",
            "json",
        ]
    )

    data = json.loads(capsys.readouterr().out)
    assert rc in {0, 1}
    assert data["config_dir"] == str(config_dir.resolve())


def test_module_help_smoke_does_not_require_cli_binaries():
    proc = subprocess.run(
        [sys.executable, "-m", "crewai_headless_flow", "--help"],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert proc.returncode == 0
    assert "doctor" in proc.stdout
    assert "preflight" in proc.stdout
