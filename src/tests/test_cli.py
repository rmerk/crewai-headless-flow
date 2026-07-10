from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from crewai_headless_flow.config import FlowConfig


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
            "runs_dir": Path("./runs").resolve(),
        }
    ]


def test_run_forwards_runs_dir_to_backend(
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
            "add tests",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--runs-dir",
            str(tmp_path / "my-runs"),
        ]
    )

    assert rc == 0
    assert calls[0]["runs_dir"] == (tmp_path / "my-runs").resolve()


def test_run_runs_dir_none_disables_run_store(
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
            "add tests",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--runs-dir",
            "none",
        ]
    )

    assert rc == 0
    assert calls[0]["runs_dir"] is None


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


@pytest.mark.parametrize("raw_value", ["0", "-1"])
def test_run_rejects_non_positive_max_revisions(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str], raw_value: str
):
    from crewai_headless_flow import cli

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--max-revisions",
            raw_value,
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "--max-revisions must be at least 1" in err


def test_run_applies_stage_overrides_without_yaml_edits(
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
            "--config-dir",
            str(config_dir),
            "--override-worker",
            "do_work=claude",
            "--override-model",
            "do_work=sonnet",
            "--override-timeout",
            "do_work=450",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]
    do_work = cfg.get_stage("do_work")

    assert rc == 0
    assert do_work.worker == "claude"
    assert do_work.model == "sonnet"
    assert do_work.timeout == 450


def test_run_applies_default_overrides_without_yaml_edits(
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
            "--config-dir",
            str(config_dir),
            "--override-default-worker",
            "claude",
            "--override-default-model",
            "sonnet",
            "--override-default-timeout",
            "450",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]
    plan = cfg.get_stage("plan")
    review = cfg.get_stage("review")

    assert rc == 0
    assert plan.worker == "claude"
    assert plan.model == "sonnet"
    assert plan.timeout == 450
    assert review.worker == "claude"
    assert review.model == "sonnet"
    assert review.timeout == 450


def test_run_applies_skill_overrides_without_yaml_edits(
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
            "--config-dir",
            str(config_dir),
            "--override-skill",
            "do_work=test-driven-development",
            "--override-skill",
            "review=doubt-driven-development",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.get_stage("do_work").skill == "test-driven-development"
    assert cfg.get_stage("review").skill == "doubt-driven-development"


def test_run_applies_human_feedback_overrides_without_yaml_edits(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback",
            "enabled=true",
            "--override-human-feedback",
            "before_plan=yes",
            "--override-human-feedback",
            "before_do_work=false",
            "--override-human-feedback",
            "before_review=true",
            "--override-human-feedback",
            "after_review=true",
            "--override-human-feedback",
            "before_finalize=false",
            "--override-human-feedback",
            "capture_instructions=yes",
            "--override-human-feedback",
            "advanced_actions=on",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["before_plan"] is True
    assert cfg.human_feedback["before_do_work"] is False
    assert cfg.human_feedback["before_review"] is True
    assert cfg.human_feedback["after_review"] is True
    assert cfg.human_feedback["before_finalize"] is False
    assert cfg.human_feedback["capture_instructions"] is True
    assert cfg.human_feedback["advanced_actions"] is True


def test_run_applies_conditional_trigger_dotted_overrides(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback",
            "enabled=true",
            "--override-human-feedback",
            "mode=conditional",
            "--override-human-feedback",
            "conditional.triggers.repeated_task_failure.enabled=true",
            "--override-human-feedback",
            "conditional.triggers.repeated_task_failure.min_attempts=3",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["mode"] == "conditional"
    triggers = cfg.human_feedback["conditional"]["triggers"]
    assert triggers["repeated_task_failure"] == {"enabled": True, "min_attempts": 3}
    # Sibling trigger keeps its defaults; only the targeted leaf changed.
    assert triggers["approaching_max_revisions"] == {"enabled": False, "within": 1}


def test_run_rejects_invalid_conditional_threshold_override(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
):
    from crewai_headless_flow import cli

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--override-human-feedback",
            "conditional.triggers.repeated_task_failure.min_attempts=0",
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "min_attempts must be a positive integer" in err


def test_run_rejects_unknown_dotted_human_feedback_override_root(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
):
    from crewai_headless_flow import cli

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--override-human-feedback",
            "bogus.nested.key=1",
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "Dotted overrides are only supported under" in err


def test_run_applies_future_human_feedback_override_when_key_exists_in_yaml(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    from crewai_headless_flow import cli

    worker_file = config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"]["future_gate"] = "default-lane"
    worker_file.write_text(yaml.safe_dump(data))

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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback",
            "future_gate=fast-lane",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["future_gate"] == "fast-lane"


def test_run_applies_human_feedback_action_overrides_without_yaml_edits(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "do_work=replan,skip-to-review,target-tasks",
            "--override-human-feedback-action",
            "review=force-pass,force-revise",
            "--override-human-feedback-action",
            "finalize=skip-finalize,rerun-review",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {
        "do_work": ["replan", "skip-to-review", "target-tasks"],
        "review": ["force-pass", "force-revise"],
        "finalize": ["skip-finalize", "rerun-review"],
    }


def test_run_applies_gate_scoped_human_feedback_action_overrides_without_yaml_edits(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "after_review=force-pass,replan,rerun-review,target-tasks",
            "--override-human-feedback-action",
            "before_review=force-revise",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {
        "after_review": ["force-pass", "replan", "rerun-review", "target-tasks"],
        "before_review": ["force-revise"],
    }


def test_run_human_feedback_action_override_none_clears_inherited_allowlist(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    from crewai_headless_flow import cli

    worker_file = config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["human_feedback"] = {
        "enabled": True,
        "after_review": True,
        "action_allowlist": {
            "after_review": ["force-pass", "replan"],
            "finalize": ["skip-finalize"],
        },
    }
    worker_file.write_text(yaml.safe_dump(data))

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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "after_review=none",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {
        "after_review": [],
        "finalize": ["skip-finalize"],
    }


def test_run_applies_stage_extra_overrides_without_yaml_edits(
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
            "--config-dir",
            str(config_dir),
            "--override-stage-extra",
            "do_work.parallel.enabled=true",
            "--override-stage-extra",
            "do_work.parallel.max_workers=4",
            "--override-stage-extra",
            "do_work.parallel.planner.enabled=true",
            "--override-stage-extra",
            "do_work.replan.enabled=true",
            "--override-stage-extra",
            "do_work.replan.on_execution_failure=true",
            "--override-stage-extra",
            "do_work.replan.on_cross_task_change=true",
            "--override-stage-extra",
            "do_work.replan.on_ambiguous_success=true",
            "--override-stage-extra",
            "do_work.replan.max_execution_replans=2",
            "--override-stage-extra",
            "review.crew.enabled=true",
            "--override-stage-extra",
            "review.crew.llm.temperature=0.4",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]
    do_work = cfg.get_stage("do_work")
    review = cfg.get_stage("review")

    assert rc == 0
    assert do_work.extra["parallel"]["enabled"] is True
    assert do_work.extra["parallel"]["max_workers"] == 4
    assert do_work.extra["parallel"]["planner"]["enabled"] is True
    assert do_work.extra["replan"]["enabled"] is True
    assert do_work.extra["replan"]["on_execution_failure"] is True
    assert do_work.extra["replan"]["on_cross_task_change"] is True
    assert do_work.extra["replan"]["on_ambiguous_success"] is True
    assert do_work.extra["replan"]["max_execution_replans"] == 2
    assert review.extra["crew"]["enabled"] is True
    assert review.extra["crew"]["llm"]["temperature"] == 0.4


def test_run_rejects_stage_extra_override_for_worker_fields(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
):
    from crewai_headless_flow import cli

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--override-stage-extra",
            "do_work.worker=claude",
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "Use --override-worker instead" in err


def test_run_rejects_unknown_stage_extra_override_path(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
):
    from crewai_headless_flow import cli

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--override-stage-extra",
            "do_work.paralel.enabled=true",
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "Unsupported stage extra path 'do_work.paralel.enabled'" in err


def test_run_rejects_invalid_stage_extra_override_type(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
):
    from crewai_headless_flow import cli

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--override-stage-extra",
            "do_work.parallel.max_workers=true",
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "stage override do_work.parallel.max_workers must be integer" in err


def test_run_rejects_do_work_always_approve_false_override(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
):
    from crewai_headless_flow import cli

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--override-stage-extra",
            "do_work.always_approve=false",
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "stage override do_work.always_approve must be set to true" in err


def test_run_rejects_review_sandbox_override_other_than_read_only(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
):
    from crewai_headless_flow import cli

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--override-stage-extra",
            "review.sandbox=workspace-write",
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "stage override review.sandbox must be set to 'read-only'" in err


def test_run_rejects_invalid_crew_process_override(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
):
    from crewai_headless_flow import cli

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--override-stage-extra",
            "review.crew.process=parallel",
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert (
        "stage override review.crew.process must be set to "
        "'sequential' or 'hierarchical'" in err
    )


def test_run_rejects_unknown_skill_override(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
):
    from crewai_headless_flow import cli

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--override-skill",
            "do_work=not-a-real-skill",
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "Unknown skill 'not-a-real-skill'" in err


def test_run_rejects_unsupported_human_feedback_action_override(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
):
    from crewai_headless_flow import cli

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "review=skip-to-review",
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "Unsupported human feedback actions for target 'review'" in err


def test_run_rejects_unknown_human_feedback_override_key(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
):
    from crewai_headless_flow import cli

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--override-human-feedback",
            "before_reveiw=true",
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "Unknown human feedback override 'before_reveiw'" in err


def test_run_rejects_action_allowlist_on_wrong_human_feedback_override_flag(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
):
    from crewai_headless_flow import cli

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--override-human-feedback",
            "action_allowlist=review=force-pass",
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "Use --override-human-feedback-action" in err


def test_run_rejects_rerun_review_override_on_before_review_gate(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
):
    from crewai_headless_flow import cli

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "before_review=rerun-review",
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "Unsupported human feedback actions for target 'before_review'" in err


def test_run_rejects_rerun_review_override_on_before_do_work_gate(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
):
    from crewai_headless_flow import cli

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "before_do_work=rerun-review",
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "Unsupported human feedback actions for target 'before_do_work'" in err


def test_run_accepts_target_tasks_override_on_before_do_work_gate(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "before_do_work=target-tasks",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {
        "before_do_work": ["target-tasks"]
    }


def test_run_accepts_replan_override_on_before_do_work_gate(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "before_do_work=replan",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {"before_do_work": ["replan"]}


def test_run_accepts_skip_to_review_override_on_before_do_work_gate(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "before_do_work=skip-to-review",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {
        "before_do_work": ["skip-to-review"]
    }


def test_run_accepts_target_tasks_override_on_before_review_gate(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "before_review=target-tasks",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {"before_review": ["target-tasks"]}


def test_run_accepts_replan_override_on_before_review_gate(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "before_review=replan",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {"before_review": ["replan"]}


def test_run_accepts_force_revise_override_on_before_review_gate(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "before_review=force-revise",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {"before_review": ["force-revise"]}


def test_run_accepts_force_pass_override_on_before_review_gate(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "before_review=force-pass",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {"before_review": ["force-pass"]}


def test_run_accepts_rerun_review_override_on_after_review_gate(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "after_review=rerun-review",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {"after_review": ["rerun-review"]}


def test_run_accepts_target_tasks_override_on_after_review_gate(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "after_review=target-tasks",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {"after_review": ["target-tasks"]}


def test_run_accepts_replan_override_on_after_review_gate(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "after_review=replan",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {"after_review": ["replan"]}


def test_run_accepts_force_revise_override_on_after_review_gate(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "after_review=force-revise",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {"after_review": ["force-revise"]}


def test_run_accepts_force_pass_override_on_after_review_gate(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "after_review=force-pass",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {"after_review": ["force-pass"]}


def test_run_accepts_rerun_review_override_on_before_finalize_gate(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "before_finalize=rerun-review",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {
        "before_finalize": ["rerun-review"]
    }


def test_run_accepts_replan_override_on_before_finalize_gate(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "before_finalize=replan",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {"before_finalize": ["replan"]}


def test_run_accepts_force_revise_override_on_before_finalize_gate(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "before_finalize=force-revise",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {
        "before_finalize": ["force-revise"]
    }


def test_run_accepts_skip_finalize_override_on_before_finalize_gate(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "before_finalize=skip-finalize",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {
        "before_finalize": ["skip-finalize"]
    }


def test_run_accepts_target_tasks_override_on_before_finalize_gate(
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
            "--config-dir",
            str(config_dir),
            "--override-human-feedback-action",
            "before_finalize=target-tasks",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["action_allowlist"] == {
        "before_finalize": ["target-tasks"]
    }


def test_run_resume_state_routes_to_resume_backend(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    from crewai_headless_flow import cli

    state_path = tmp_path / "aborted-state.json"
    state_path.write_text(
        json.dumps(
            {
                "request": "ship cli",
                "target_repo": str(tmp_path),
                "status": "aborted_by_human",
                "aborted_checkpoint": {"stage": "do_work"},
                "spec": "resume it",
                "tasks": [],
                "changed_files": [],
                "issues": [],
                "review_task_hints": [],
                "human_feedback_log": [],
                "resolved_stages": [],
                "history": [],
                "revisions": 0,
                "max_revisions": 2,
                "review_status": "pending",
            }
        )
    )

    calls: list[dict] = []

    def fail_if_run_headless_flow(**kwargs):
        raise AssertionError("fresh run path should not be used for resume")

    def fake_resume_headless_flow(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status="completed",
            model_dump=lambda: {"status": "completed", "revisions": 0},
        )

    monkeypatch.setattr(cli, "run_headless_flow", fail_if_run_headless_flow)
    monkeypatch.setattr(cli, "resume_headless_flow", fake_resume_headless_flow)

    rc = cli.main(
        [
            "run",
            "--resume-state-file",
            str(state_path),
            "--config-dir",
            str(config_dir),
        ]
    )

    assert rc == 0
    assert calls[0]["state"].status == "aborted_by_human"
    assert calls[0]["state"].aborted_stage == "do_work"


def test_run_resume_state_applies_max_revisions_override(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    from crewai_headless_flow import cli

    state_path = tmp_path / "aborted-state.json"
    state_path.write_text(
        json.dumps(
            {
                "request": "ship cli",
                "target_repo": str(tmp_path),
                "status": "aborted_by_human",
                "aborted_stage": "do_work",
                "spec": "resume it",
                "tasks": [],
                "changed_files": [],
                "issues": [],
                "review_task_hints": [],
                "human_feedback_log": [],
                "resolved_stages": [],
                "history": [],
                "revisions": 0,
                "max_revisions": 2,
                "review_status": "pending",
            }
        )
    )

    calls: list[dict] = []

    def fake_resume_headless_flow(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status="completed",
            model_dump=lambda: {
                "status": "completed",
                "revisions": 0,
                "max_revisions": kwargs["state"].max_revisions,
            },
        )

    monkeypatch.setattr(cli, "resume_headless_flow", fake_resume_headless_flow)

    rc = cli.main(
        [
            "run",
            "--resume-state-file",
            str(state_path),
            "--config-dir",
            str(config_dir),
            "--max-revisions",
            "5",
        ]
    )

    assert rc == 0
    assert calls[0]["state"].max_revisions == 5


def test_run_resume_state_applies_runtime_overrides_to_resolved_config(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    from crewai_headless_flow import cli

    state_path = tmp_path / "aborted-state.json"
    state_path.write_text(
        json.dumps(
            {
                "request": "ship cli",
                "target_repo": str(tmp_path),
                "status": "aborted_by_human",
                "aborted_stage": "do_work",
                "spec": "resume it",
                "tasks": [],
                "changed_files": [],
                "issues": [],
                "review_task_hints": [],
                "human_feedback_log": [],
                "resolved_stages": [],
                "history": [],
                "revisions": 0,
                "max_revisions": 2,
                "review_status": "pending",
            }
        )
    )

    calls: list[dict] = []

    def fake_resume_headless_flow(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status="completed",
            model_dump=lambda: {"status": "completed", "revisions": 0},
        )

    monkeypatch.setattr(cli, "resume_headless_flow", fake_resume_headless_flow)

    rc = cli.main(
        [
            "run",
            "--resume-state-file",
            str(state_path),
            "--config-dir",
            str(config_dir),
            "--override-skill",
            "do_work=test-driven-development",
            "--override-default-worker",
            "gemini",
            "--override-default-model",
            "gemini-2.5-pro",
            "--override-default-timeout",
            "600",
            "--override-worker",
            "do_work=claude",
            "--override-model",
            "do_work=sonnet",
            "--override-timeout",
            "do_work=450",
            "--override-stage-extra",
            "do_work.parallel.enabled=true",
            "--override-human-feedback",
            "enabled=true",
            "--override-human-feedback",
            "after_review=true",
            "--override-human-feedback-action",
            "after_review=force-pass,replan",
        ]
    )

    cfg: FlowConfig = calls[0]["config"]
    plan = cfg.get_stage("plan")
    do_work = cfg.get_stage("do_work")
    review = cfg.get_stage("review")

    assert rc == 0
    assert calls[0]["state"].status == "aborted_by_human"
    assert plan.worker == "gemini"
    assert plan.model == "gemini-2.5-pro"
    assert plan.timeout == 600
    assert review.worker == "gemini"
    assert review.model == "gemini-2.5-pro"
    assert review.timeout == 600
    assert do_work.skill == "test-driven-development"
    assert do_work.worker == "claude"
    assert do_work.model == "sonnet"
    assert do_work.timeout == 450
    assert do_work.extra["parallel"]["enabled"] is True
    assert cfg.human_feedback["enabled"] is True
    assert cfg.human_feedback["after_review"] is True
    assert cfg.human_feedback["action_allowlist"] == {
        "after_review": ["force-pass", "replan"]
    }


def test_run_state_file_persists_effective_config_dir(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    from crewai_headless_flow import cli

    state_path = tmp_path / "completed-state.json"

    def fake_run_headless_flow(**kwargs):
        return SimpleNamespace(
            status="completed",
            model_dump=lambda: {"status": "completed", "revisions": 0},
        )

    monkeypatch.setattr(cli, "run_headless_flow", fake_run_headless_flow)

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--state-file",
            str(state_path),
        ]
    )

    data = json.loads(state_path.read_text())

    assert rc == 0
    assert data["config_dir"] == str(config_dir.resolve())


def test_run_resume_state_uses_saved_config_dir_when_flag_omitted(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    from crewai_headless_flow import cli

    state_path = tmp_path / "aborted-state.json"
    state_path.write_text(
        json.dumps(
            {
                "request": "ship cli",
                "target_repo": str(tmp_path),
                "config_dir": str(config_dir.resolve()),
                "status": "aborted_by_human",
                "aborted_stage": "do_work",
                "spec": "resume it",
                "tasks": [],
                "changed_files": [],
                "issues": [],
                "review_task_hints": [],
                "human_feedback_log": [],
                "resolved_stages": [],
                "history": [],
                "revisions": 0,
                "max_revisions": 2,
                "review_status": "pending",
            }
        )
    )

    captured: dict[str, Path] = {}
    real_load_runtime_config = cli.load_runtime_config

    def fake_load_runtime_config(**kwargs):
        captured["config_dir"] = kwargs["config_dir"]
        return real_load_runtime_config(**kwargs)

    def fake_run_preflight(target_repo, config_dir=None):
        captured["preflight_config_dir"] = config_dir
        return SimpleNamespace(status="pass", failures=[])

    def fake_resume_headless_flow(**kwargs):
        return SimpleNamespace(
            status="completed",
            model_dump=lambda: {"status": "completed", "revisions": 0},
        )

    monkeypatch.setattr(cli, "load_runtime_config", fake_load_runtime_config)
    monkeypatch.setattr(cli, "run_preflight", fake_run_preflight)
    monkeypatch.setattr(cli, "resume_headless_flow", fake_resume_headless_flow)

    rc = cli.main(
        [
            "run",
            "--resume-state-file",
            str(state_path),
        ]
    )

    assert rc == 0
    assert captured["config_dir"] == config_dir.resolve()
    assert captured["preflight_config_dir"] == config_dir.resolve()


def test_run_resume_state_explicit_config_dir_overrides_saved_config_dir(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    from crewai_headless_flow import cli

    override_config = tmp_path / "override-config"
    override_config.mkdir()
    (override_config / "skills.yaml").write_text(
        (config_dir / "skills.yaml").read_text()
    )
    (override_config / "worker.yaml").write_text(
        (config_dir / "worker.yaml").read_text()
    )

    state_path = tmp_path / "aborted-state.json"
    state_path.write_text(
        json.dumps(
            {
                "request": "ship cli",
                "target_repo": str(tmp_path),
                "config_dir": str(config_dir.resolve()),
                "status": "aborted_by_human",
                "aborted_stage": "do_work",
                "spec": "resume it",
                "tasks": [],
                "changed_files": [],
                "issues": [],
                "review_task_hints": [],
                "human_feedback_log": [],
                "resolved_stages": [],
                "history": [],
                "revisions": 0,
                "max_revisions": 2,
                "review_status": "pending",
            }
        )
    )

    captured: dict[str, Path] = {}
    real_load_runtime_config = cli.load_runtime_config

    def fake_load_runtime_config(**kwargs):
        captured["config_dir"] = kwargs["config_dir"]
        return real_load_runtime_config(**kwargs)

    def fake_run_preflight(target_repo, config_dir=None):
        captured["preflight_config_dir"] = config_dir
        return SimpleNamespace(status="pass", failures=[])

    def fake_resume_headless_flow(**kwargs):
        return SimpleNamespace(
            status="completed",
            model_dump=lambda: {"status": "completed", "revisions": 0},
        )

    monkeypatch.setattr(cli, "load_runtime_config", fake_load_runtime_config)
    monkeypatch.setattr(cli, "run_preflight", fake_run_preflight)
    monkeypatch.setattr(cli, "resume_headless_flow", fake_resume_headless_flow)

    rc = cli.main(
        [
            "run",
            "--resume-state-file",
            str(state_path),
            "--config-dir",
            str(override_config),
        ]
    )

    assert rc == 0
    assert captured["config_dir"] == override_config.resolve()
    assert captured["preflight_config_dir"] == override_config.resolve()


def test_run_resume_state_rejects_request_or_target_mix(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    from crewai_headless_flow import cli

    state_path = tmp_path / "aborted-state.json"
    state_path.write_text(
        json.dumps(
            {
                "request": "ship cli",
                "target_repo": str(tmp_path),
                "status": "aborted_by_human",
                "aborted_stage": "do_work",
                "spec": "resume it",
                "tasks": [],
                "changed_files": [],
                "issues": [],
                "review_task_hints": [],
                "human_feedback_log": [],
                "resolved_stages": [],
                "history": [],
                "revisions": 0,
                "max_revisions": 2,
                "review_status": "pending",
            }
        )
    )

    rc = cli.main(
        [
            "run",
            "--resume-state-file",
            str(state_path),
            "--request",
            "fresh request",
            "--config-dir",
            str(config_dir),
        ]
    )

    err = capsys.readouterr().err
    assert rc == 1
    assert "--resume-state-file cannot be combined" in err


def test_run_json_output_is_parseable(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    from crewai_headless_flow import cli

    def fake_run_headless_flow(**kwargs):
        return SimpleNamespace(
            status="completed",
            final_artifact="done",
            debug_report="# Flow Execution Report\nok",
            model_dump=lambda: {
                "status": "completed",
                "final_artifact": "done",
                "debug_report": "# Flow Execution Report\nok",
            },
        )

    monkeypatch.setattr(cli, "run_headless_flow", fake_run_headless_flow)

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--format",
            "json",
        ]
    )

    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["status"] == "completed"
    assert data["debug_report"].startswith("# Flow Execution Report")


def test_run_writes_debug_report_file(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    from crewai_headless_flow import cli

    report_path = tmp_path / "debug-report.md"

    def fake_run_headless_flow(**kwargs):
        return SimpleNamespace(
            status="completed",
            debug_report="# Flow Execution Report\nok",
            model_dump=lambda: {
                "status": "completed",
                "debug_report": "# Flow Execution Report\nok",
            },
        )

    monkeypatch.setattr(cli, "run_headless_flow", fake_run_headless_flow)

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--debug-report-file",
            str(report_path),
        ]
    )

    assert rc == 0
    assert report_path.read_text() == "# Flow Execution Report\nok"


def test_run_writes_state_file(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    from crewai_headless_flow import cli

    state_path = tmp_path / "run-state.json"

    def fake_run_headless_flow(**kwargs):
        return SimpleNamespace(
            status="completed",
            final_artifact="done",
            debug_report="# Flow Execution Report\nok",
            model_dump=lambda: {
                "status": "completed",
                "final_artifact": "done",
                "debug_report": "# Flow Execution Report\nok",
                "resolved_stages": [
                    {
                        "stage": "plan",
                        "skill": "planning-and-task-breakdown",
                        "worker": "codex",
                        "model": None,
                        "timeout": 300,
                        "extra": {},
                        "can_mutate": False,
                    }
                ],
                "tasks": [{"id": 1, "status": "done"}],
                "task_executions": [
                    {
                        "task_id": 1,
                        "attempt": 1,
                        "revision": 0,
                        "worker": "codex",
                        "model": None,
                        "orchestration": "direct",
                        "success": True,
                        "summary": "done",
                        "error": None,
                        "changed_files": ["src/a.py"],
                        "isolated_workspace": False,
                        "workspace": None,
                        "parallel_batch_id": None,
                        "crew_rounds": [],
                    }
                ],
                "changed_files": ["src/a.py"],
                "revisions": 1,
                "max_revisions": 2,
            },
        )

    monkeypatch.setattr(cli, "run_headless_flow", fake_run_headless_flow)

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            "--state-file",
            str(state_path),
        ]
    )

    assert rc == 0
    data = json.loads(state_path.read_text())
    assert data["status"] == "completed"
    assert data["resolved_stages"][0]["worker"] == "codex"
    assert data["tasks"][0]["status"] == "done"
    assert data["task_executions"][0]["task_id"] == 1


def test_run_text_output_is_compact_summary(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    from crewai_headless_flow import cli

    def fake_run_headless_flow(**kwargs):
        return SimpleNamespace(
            status="completed",
            final_artifact="done",
            debug_report="# Flow Execution Report\nverbose",
            model_dump=lambda: {
                "status": "completed",
                "final_artifact": "done",
                "debug_report": "# Flow Execution Report\nverbose",
                "tasks": [{"id": 1, "status": "done"}, {"id": 2, "status": "pending"}],
                "changed_files": ["src/a.py", "src/b.py"],
                "revisions": 1,
                "max_revisions": 2,
            },
        )

    monkeypatch.setattr(cli, "run_headless_flow", fake_run_headless_flow)

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
        ]
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "Status: completed" in out
    assert "Revisions: 1/2" in out
    assert "Tasks: 1/2 done" in out
    assert "Changed files tracked: 2" in out
    assert "Debug Report: available via --format json or --debug-report-file" in out
    assert "# Flow Execution Report\nverbose" not in out


def test_run_text_output_includes_compact_failure_reasons(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    from crewai_headless_flow import cli

    def fake_run_headless_flow(**kwargs):
        return SimpleNamespace(
            status="failed",
            model_dump=lambda: {
                "status": "failed",
                "tasks": [{"id": 1, "status": "done"}, {"id": 2, "status": "pending"}],
                "changed_files": ["src/a.py"],
                "revisions": 2,
                "max_revisions": 2,
                "issues": [
                    "Max revisions reached before review could pass.",
                    "Structured tasks remain incomplete: 2",
                ],
                "errors": [
                    "Max revisions reached before review could pass.",
                    "Another lower-priority error",
                ],
            },
        )

    monkeypatch.setattr(cli, "run_headless_flow", fake_run_headless_flow)

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
        ]
    )

    out = capsys.readouterr().out
    assert rc == 1
    assert "Status: failed" in out
    assert "Revisions: 2/2" in out
    assert "Tasks: 1/2 done" in out
    assert "Changed files tracked: 1" in out
    assert "Issues:" in out
    assert "- Max revisions reached before review could pass." in out
    assert "- Structured tasks remain incomplete: 2" in out
    assert "Errors:" in out
    assert "- Another lower-priority error" in out


def test_run_text_output_shows_abort_and_replan_context(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    from crewai_headless_flow import cli

    def fake_run_headless_flow(**kwargs):
        return SimpleNamespace(
            status="aborted_by_human",
            model_dump=lambda: {
                "status": "aborted_by_human",
                "tasks": [{"id": 1, "status": "done"}, {"id": 2, "status": "pending"}],
                "changed_files": ["src/a.py"],
                "revisions": 1,
                "max_revisions": 2,
                "pending_revision_replan": True,
                "pending_revision_replan_reason": (
                    "Human requested replanning before the next revise loop."
                ),
                "aborted_checkpoint": {
                    "stage": "review",
                    "gate": "after_review",
                    "message": "Automated review completed.\nSuggested status: revise",
                    "before_review_instructions": "Focus on migrations",
                    "stage_input": "Summary line\nSecond line",
                },
            },
        )

    monkeypatch.setattr(cli, "run_headless_flow", fake_run_headless_flow)

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
        ]
    )

    out = capsys.readouterr().out
    assert rc == 1
    assert "Status: aborted_by_human" in out
    assert "Aborted checkpoint: review@after_review" in out
    assert "Abort message: Automated review completed. Suggested status: revise" in out
    assert "Saved before_review instructions: yes" in out
    assert "Resume input captured: yes" in out
    assert (
        "Pending revision replan: Human requested replanning before the next revise loop."
        in out
    )


def test_run_text_output_shows_latest_review_input_for_aborted_finalize(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    from crewai_headless_flow import cli

    def fake_run_headless_flow(**kwargs):
        return SimpleNamespace(
            status="aborted_by_human",
            model_dump=lambda: {
                "status": "aborted_by_human",
                "tasks": [],
                "changed_files": [],
                "revisions": 0,
                "max_revisions": 2,
                "latest_work_summary": "Implemented API cleanup\nAdded regression tests",
                "aborted_checkpoint": {
                    "stage": "finalize",
                    "gate": "before_finalize",
                    "message": "About to finalize and write documentation/ADR. This is the last step.",
                    "stage_input": "pass",
                },
            },
        )

    monkeypatch.setattr(cli, "run_headless_flow", fake_run_headless_flow)

    rc = cli.main(
        [
            "run",
            "--request",
            "ship cli",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
        ]
    )

    out = capsys.readouterr().out
    assert rc == 1
    assert "Aborted checkpoint: finalize@before_finalize" in out
    assert "Resume input captured: yes" in out
    assert "Latest review input captured: yes" in out


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
        "finalize": {"worker": "cursor"},
    }
    worker_file.write_text(yaml.safe_dump(data))

    monkeypatch.setattr(cli.diagnostics.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        cli.diagnostics,
        "_run_probe",
        lambda cmd, timeout=3: cli.diagnostics.ProbeResult(
            returncode=0,
            stdout=" ".join(cmd)
            + " --sandbox --output-schema --always-approve --output-format --permission-mode --json-schema --prompt --approval-mode --print --output-format --plan --force --trust --workspace --model",
            stderr="",
        ),
    )
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)

    rc = cli.main(["doctor", "--config-dir", str(config_dir), "--format", "json"])

    data = json.loads(capsys.readouterr().out)
    check_names = {check["name"] for check in data["checks"]}
    assert rc == 0
    assert {"cli.codex", "cli.grok", "cli.claude", "cli.cursor"}.issubset(check_names)


def test_doctor_applies_runtime_worker_override_for_cli_validation(
    config_dir: Path, monkeypatch, capsys
):
    from crewai_headless_flow import cli

    monkeypatch.setattr(cli.diagnostics.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        cli.diagnostics,
        "_run_probe",
        lambda cmd, timeout=3: cli.diagnostics.ProbeResult(
            returncode=0,
            stdout=" ".join(cmd)
            + " --sandbox --output-schema --always-approve --output-format --permission-mode --json-schema --prompt --approval-mode",
            stderr="",
        ),
    )

    rc = cli.main(
        [
            "doctor",
            "--config-dir",
            str(config_dir),
            "--override-worker",
            "finalize=gemini",
            "--format",
            "json",
        ]
    )

    data = json.loads(capsys.readouterr().out)
    check_names = {check["name"] for check in data["checks"]}
    load_check = next(
        check for check in data["checks"] if check["name"] == "config.load"
    )

    assert rc == 0
    assert "cli.gemini" in check_names
    assert load_check["details"]["overrides_applied"] is True


def test_doctor_applies_runtime_default_override_for_cli_validation(
    config_dir: Path, monkeypatch, capsys
):
    from crewai_headless_flow import cli

    monkeypatch.setattr(cli.diagnostics.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        cli.diagnostics,
        "_run_probe",
        lambda cmd, timeout=3: cli.diagnostics.ProbeResult(
            returncode=0,
            stdout=" ".join(cmd)
            + " --sandbox --output-schema --always-approve --output-format --permission-mode --json-schema --prompt --approval-mode",
            stderr="",
        ),
    )

    rc = cli.main(
        [
            "doctor",
            "--config-dir",
            str(config_dir),
            "--override-default-worker",
            "gemini",
            "--format",
            "json",
        ]
    )

    data = json.loads(capsys.readouterr().out)
    check_names = {check["name"] for check in data["checks"]}
    load_check = next(
        check for check in data["checks"] if check["name"] == "config.load"
    )

    assert rc == 0
    assert "cli.gemini" in check_names
    assert load_check["details"]["overrides_applied"] is True


def test_doctor_applies_runtime_stage_extra_override_for_crew_validation(
    config_dir: Path, monkeypatch, capsys
):
    from crewai_headless_flow import cli

    monkeypatch.setattr(cli.diagnostics.shutil, "which", lambda name: f"/bin/{name}")

    def fake_probe(cmd, timeout=3):
        if cmd[0] == "ollama":
            return cli.diagnostics.ProbeResult(
                returncode=0,
                stdout="llama3.2",
                stderr="",
            )
        return cli.diagnostics.ProbeResult(
            returncode=0,
            stdout=" ".join(cmd)
            + " --sandbox --output-schema --always-approve --output-format --permission-mode --json-schema --prompt --approval-mode",
            stderr="",
        )

    monkeypatch.setattr(cli.diagnostics, "_run_probe", fake_probe)

    rc = cli.main(
        [
            "doctor",
            "--config-dir",
            str(config_dir),
            "--override-stage-extra",
            "review.crew.enabled=true",
            "--format",
            "json",
        ]
    )

    data = json.loads(capsys.readouterr().out)
    check_names = {check["name"] for check in data["checks"]}

    assert rc == 0
    assert "config.review_crew" in check_names
    assert "cli.ollama" in check_names


def test_doctor_skips_ollama_for_custom_provider_stage_extra_override(
    config_dir: Path, monkeypatch, capsys
):
    from crewai_headless_flow import cli

    monkeypatch.setattr(
        cli.diagnostics.shutil,
        "which",
        lambda name: None if name == "ollama" else f"/bin/{name}",
    )
    monkeypatch.setattr(
        cli.diagnostics,
        "_run_probe",
        lambda cmd, timeout=3: cli.diagnostics.ProbeResult(
            returncode=0,
            stdout=" ".join(cmd)
            + " --sandbox --output-schema --always-approve --output-format --permission-mode --json-schema --prompt --approval-mode",
            stderr="",
        ),
    )

    rc = cli.main(
        [
            "doctor",
            "--config-dir",
            str(config_dir),
            "--override-stage-extra",
            "review.crew.enabled=true",
            "--override-stage-extra",
            "review.crew.llm.model=gpt-4o-mini",
            "--override-stage-extra",
            "review.crew.llm.base_url=https://api.openai.com/v1",
            "--format",
            "json",
        ]
    )

    data = json.loads(capsys.readouterr().out)
    check_names = {check["name"] for check in data["checks"]}
    review_check = next(
        check for check in data["checks"] if check["name"] == "config.review_crew"
    )

    assert rc == 0
    assert data["status"] == "warn"
    assert "config.review_crew" in check_names
    assert "cli.ollama" not in check_names
    assert review_check["details"]["ollama_required"] is False


def test_doctor_json_includes_resolved_runtime_for_stage_extra_overrides(
    config_dir: Path, monkeypatch, capsys
):
    from crewai_headless_flow import cli

    monkeypatch.setattr(
        cli.diagnostics.shutil,
        "which",
        lambda name: None if name == "ollama" else f"/bin/{name}",
    )
    monkeypatch.setattr(
        cli.diagnostics,
        "_run_probe",
        lambda cmd, timeout=3: cli.diagnostics.ProbeResult(
            returncode=0,
            stdout=" ".join(cmd)
            + " --sandbox --output-schema --always-approve --output-format --permission-mode --json-schema --prompt --approval-mode",
            stderr="",
        ),
    )

    rc = cli.main(
        [
            "doctor",
            "--config-dir",
            str(config_dir),
            "--override-stage-extra",
            "do_work.parallel.enabled=true",
            "--override-stage-extra",
            "do_work.parallel.max_workers=4",
            "--override-stage-extra",
            "review.crew.enabled=true",
            "--override-stage-extra",
            "review.crew.llm.model=gpt-4o-mini",
            "--override-stage-extra",
            "review.crew.llm.base_url=https://api.openai.com/v1",
            "--format",
            "json",
        ]
    )

    data = json.loads(capsys.readouterr().out)
    load_check = next(
        check for check in data["checks"] if check["name"] == "config.load"
    )
    resolved_stages = {
        stage["stage"]: stage for stage in data["resolved_runtime"]["stages"]
    }

    assert rc == 0
    assert load_check["details"]["overrides_applied"] is True
    assert resolved_stages["do_work"]["runtime_knobs"] == {
        "parallel": {"enabled": True, "max_workers": 4}
    }
    assert resolved_stages["review"]["notes"] == ["crew_llm_provider=external/custom"]
    assert resolved_stages["review"]["runtime_knobs"]["crew"]["enabled"] is True
    assert resolved_stages["review"]["runtime_knobs"]["crew"]["llm"] == {
        "model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1",
    }


def test_doctor_text_output_includes_resolved_runtime_summary(
    config_dir: Path, monkeypatch, capsys
):
    from crewai_headless_flow import cli

    monkeypatch.setattr(
        cli.diagnostics.shutil,
        "which",
        lambda name: None if name == "ollama" else f"/bin/{name}",
    )
    monkeypatch.setattr(
        cli.diagnostics,
        "_run_probe",
        lambda cmd, timeout=3: cli.diagnostics.ProbeResult(
            returncode=0,
            stdout=" ".join(cmd)
            + " --sandbox --output-schema --always-approve --output-format --permission-mode --json-schema --prompt --approval-mode",
            stderr="",
        ),
    )

    rc = cli.main(
        [
            "doctor",
            "--config-dir",
            str(config_dir),
            "--override-stage-extra",
            "do_work.parallel.enabled=true",
            "--override-stage-extra",
            "do_work.parallel.max_workers=4",
            "--override-stage-extra",
            "review.crew.enabled=true",
            "--override-stage-extra",
            "review.crew.llm.model=gpt-4o-mini",
            "--override-stage-extra",
            "review.crew.llm.base_url=https://api.openai.com/v1",
            "--override-human-feedback",
            "after_review=true",
            "--override-human-feedback-action",
            "after_review=replan,rerun-review,target-tasks,force-revise,force-pass",
        ]
    )

    out = capsys.readouterr().out

    assert rc == 0
    assert f"Config dir: {config_dir.resolve()}" in out
    assert "Resolved Runtime:" in out
    assert (
        "- do_work: skill=incremental-implementation | worker=codex | "
        "model=(default) | timeout=300s | mutates=yes"
    ) in out
    assert 'Runtime knobs: {"parallel": {"enabled": true, "max_workers": 4}}' in out
    assert (
        "- review: skill=code-review-and-quality | worker=codex | "
        "model=(default) | timeout=300s | mutates=no"
    ) in out
    assert "Notes: crew_llm_provider=external/custom" in out
    assert '"after_review": true' in out
    assert (
        '"action_allowlist": {"after_review": ["replan", "rerun-review", '
        '"target-tasks", "force-revise", "force-pass"]}'
    ) in out
    assert "Status: warn" in out


def test_doctor_reports_runtime_override_resolution_failures_as_json_report(
    config_dir: Path, capsys
):
    from crewai_headless_flow import cli

    rc = cli.main(
        [
            "doctor",
            "--config-dir",
            str(config_dir),
            "--override-human-feedback",
            "before_reveiw=true",
            "--format",
            "json",
        ]
    )

    data = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert data["status"] == "fail"
    assert any(
        "Unknown human feedback override 'before_reveiw'" in failure
        for failure in data["failures"]
    )


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


def test_preflight_text_output_includes_git_and_tooling_summary(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    from crewai_headless_flow import cli

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "README.md").write_text("# x\n")

    monkeypatch.setattr(cli.diagnostics.shutil, "which", lambda name: "/bin/git")

    def fake_probe(cmd, timeout=3):
        if "rev-parse" in cmd:
            if "--is-inside-work-tree" in cmd:
                return cli.diagnostics.ProbeResult(
                    returncode=0, stdout="true\n", stderr=""
                )
            if "--abbrev-ref" in cmd:
                return cli.diagnostics.ProbeResult(
                    returncode=0, stdout="main\n", stderr=""
                )
        if "status" in cmd:
            return cli.diagnostics.ProbeResult(
                returncode=0,
                stdout=" M src/app.py\n?? notes.txt\n",
                stderr="",
            )
        return cli.diagnostics.ProbeResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli.diagnostics, "_run_probe", fake_probe)

    rc = cli.main(
        [
            "preflight",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
        ]
    )

    out = capsys.readouterr().out

    assert rc == 0
    assert f"Config dir: {config_dir.resolve()}" in out
    assert f"Target repo: {tmp_path.resolve()}" in out
    assert "Tooling: 2/5 present" in out
    assert "Missing tooling: uv.lock, package.json, pytest.ini" in out
    assert (
        "Git: dirty | branch=main | staged=no | unstaged=yes | "
        "untracked=yes | conflicts=no"
    ) in out
    assert "Git porcelain:" in out
    assert "- M src/app.py" in out
    assert "- ?? notes.txt" in out
    assert "Status: warn" in out


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
