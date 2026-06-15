from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest


pytestmark = pytest.mark.offline


REPO_ROOT = Path(__file__).resolve().parents[2]


def _example_config_dir(name: str) -> Path:
    return REPO_ROOT / "examples" / "configs" / name


def _patch_clean_doctor_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from crewai_headless_flow.diagnostics import ProbeResult

    help_flags = {
        "codex": "--sandbox --output-schema",
        "grok": "--always-approve --output-format",
        "claude": "--permission-mode --json-schema",
        "gemini": "--prompt --approval-mode --output-format",
    }

    monkeypatch.setattr(
        "crewai_headless_flow.diagnostics.shutil.which", lambda name: f"/bin/{name}"
    )

    def fake_probe(cmd, timeout=3):
        binary = cmd[0]
        if binary == "ollama":
            return ProbeResult(returncode=0, stdout="llama3.2\n", stderr="")
        return ProbeResult(returncode=0, stdout=help_flags[binary], stderr="")

    monkeypatch.setattr("crewai_headless_flow.diagnostics._run_probe", fake_probe)


def _patch_clean_preflight_git(monkeypatch: pytest.MonkeyPatch) -> None:
    from crewai_headless_flow.diagnostics import ProbeResult

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


@pytest.mark.parametrize(
    ("config_dir", "expected"),
    [
        (
            REPO_ROOT / "config",
            {
                "status": "pass",
                "do_work_worker": "grok",
                "human_feedback_enabled": False,
            },
        ),
        (
            _example_config_dir("claude-do-work"),
            {
                "status": "pass",
                "do_work_worker": "claude",
                "do_work_model": "sonnet",
            },
        ),
        (
            _example_config_dir("gemini-do-work"),
            {
                "status": "pass",
                "do_work_worker": "gemini",
                "do_work_model": "gemini-2.5-pro",
            },
        ),
        (
            _example_config_dir("implementation-crew"),
            {
                "status": "warn",
                "do_work_crew_enabled": True,
                "requires_check": "config.do_work_crew",
            },
        ),
        (
            _example_config_dir("implementation-crew-parallel"),
            {
                "status": "warn",
                "do_work_crew_enabled": True,
                "do_work_parallel_enabled": True,
                "do_work_parallel_planner_enabled": False,
                "do_work_replan_enabled": False,
                "requires_check": "config.do_work_crew",
            },
        ),
        (
            _example_config_dir("implementation-crew-parallel-planner"),
            {
                "status": "warn",
                "do_work_crew_enabled": True,
                "do_work_parallel_enabled": True,
                "do_work_parallel_planner_enabled": True,
                "do_work_replan_enabled": False,
                "requires_check": "config.do_work_crew",
            },
        ),
        (
            _example_config_dir("implementation-crew-parallel-replan"),
            {
                "status": "warn",
                "do_work_crew_enabled": True,
                "do_work_parallel_enabled": True,
                "do_work_parallel_planner_enabled": True,
                "do_work_replan_enabled": True,
                "requires_check": "config.do_work_crew",
            },
        ),
        (
            _example_config_dir("plan-gate"),
            {
                "status": "pass",
                "human_feedback_before_plan": True,
                "human_feedback_capture_instructions": True,
            },
        ),
        (
            _example_config_dir("planning-crew"),
            {
                "status": "warn",
                "plan_crew_enabled": True,
                "requires_check": "config.plan_crew",
            },
        ),
        (
            _example_config_dir("review-crew"),
            {
                "status": "warn",
                "review_crew_enabled": True,
                "requires_check": "config.review_crew",
            },
        ),
        (
            _example_config_dir("guided-operator-loop"),
            {
                "status": "pass",
                "human_feedback_after_review": True,
                "human_feedback_action_allowlist": {
                    "do_work": ["replan", "skip-to-review", "target-tasks"],
                    "after_review": [
                        "replan",
                        "rerun-review",
                        "target-tasks",
                        "force-revise",
                        "force-pass",
                    ],
                    "finalize": [
                        "skip-finalize",
                        "rerun-review",
                        "force-revise",
                        "replan",
                        "target-tasks",
                    ],
                },
            },
        ),
        (
            _example_config_dir("do-work-targeting-gate"),
            {
                "status": "pass",
                "human_feedback_action_allowlist": {"before_do_work": ["target-tasks"]},
            },
        ),
        (
            _example_config_dir("do-work-replan-gate"),
            {
                "status": "pass",
                "human_feedback_before_do_work": True,
                "human_feedback_action_allowlist": {"before_do_work": ["replan"]},
            },
        ),
        (
            _example_config_dir("do-work-skip-to-review-gate"),
            {
                "status": "pass",
                "human_feedback_before_do_work": True,
                "human_feedback_action_allowlist": {
                    "before_do_work": ["skip-to-review"]
                },
            },
        ),
        (
            _example_config_dir("operator-review-gate"),
            {
                "status": "pass",
                "human_feedback_after_review": True,
                "human_feedback_action_allowlist": {
                    "after_review": ["rerun-review", "force-revise", "force-pass"]
                },
            },
        ),
        (
            _example_config_dir("review-rerun-review-gate"),
            {
                "status": "pass",
                "human_feedback_after_review": True,
                "human_feedback_action_allowlist": {"after_review": ["rerun-review"]},
            },
        ),
        (
            _example_config_dir("review-targeting-only-gate"),
            {
                "status": "pass",
                "human_feedback_after_review": True,
                "human_feedback_action_allowlist": {"after_review": ["target-tasks"]},
            },
        ),
        (
            _example_config_dir("review-replan-only-gate"),
            {
                "status": "pass",
                "human_feedback_after_review": True,
                "human_feedback_action_allowlist": {"after_review": ["replan"]},
            },
        ),
        (
            _example_config_dir("review-force-revise-gate"),
            {
                "status": "pass",
                "human_feedback_after_review": True,
                "human_feedback_action_allowlist": {"after_review": ["force-revise"]},
            },
        ),
        (
            _example_config_dir("review-force-pass-gate"),
            {
                "status": "pass",
                "human_feedback_after_review": True,
                "human_feedback_action_allowlist": {"after_review": ["force-pass"]},
            },
        ),
        (
            _example_config_dir("finalize-review-gate"),
            {
                "status": "pass",
                "human_feedback_before_finalize": True,
                "human_feedback_action_allowlist": {
                    "before_finalize": [
                        "rerun-review",
                        "force-revise",
                        "replan",
                        "target-tasks",
                        "skip-finalize",
                    ]
                },
            },
        ),
        (
            _example_config_dir("finalize-rerun-review-gate"),
            {
                "status": "pass",
                "human_feedback_before_finalize": True,
                "human_feedback_action_allowlist": {
                    "before_finalize": ["rerun-review"]
                },
            },
        ),
        (
            _example_config_dir("finalize-replan-gate"),
            {
                "status": "pass",
                "human_feedback_before_finalize": True,
                "human_feedback_action_allowlist": {"before_finalize": ["replan"]},
            },
        ),
        (
            _example_config_dir("finalize-force-revise-gate"),
            {
                "status": "pass",
                "human_feedback_before_finalize": True,
                "human_feedback_action_allowlist": {
                    "before_finalize": ["force-revise"]
                },
            },
        ),
        (
            _example_config_dir("finalize-skip-gate"),
            {
                "status": "pass",
                "human_feedback_before_finalize": True,
                "human_feedback_action_allowlist": {
                    "before_finalize": ["skip-finalize"]
                },
            },
        ),
        (
            _example_config_dir("finalize-targeting-gate"),
            {
                "status": "pass",
                "human_feedback_before_finalize": True,
                "human_feedback_action_allowlist": {
                    "before_finalize": ["target-tasks"]
                },
            },
        ),
        (
            _example_config_dir("review-targeting-gate"),
            {
                "status": "pass",
                "human_feedback_after_review": True,
                "human_feedback_action_allowlist": {
                    "after_review": [
                        "rerun-review",
                        "target-tasks",
                        "force-revise",
                        "force-pass",
                    ]
                },
            },
        ),
        (
            _example_config_dir("review-targeting-before-gate"),
            {
                "status": "pass",
                "human_feedback_before_review": True,
                "human_feedback_action_allowlist": {"before_review": ["target-tasks"]},
            },
        ),
        (
            _example_config_dir("review-force-revise-before-gate"),
            {
                "status": "pass",
                "human_feedback_before_review": True,
                "human_feedback_action_allowlist": {"before_review": ["force-revise"]},
            },
        ),
        (
            _example_config_dir("review-force-pass-before-gate"),
            {
                "status": "pass",
                "human_feedback_before_review": True,
                "human_feedback_action_allowlist": {"before_review": ["force-pass"]},
            },
        ),
        (
            _example_config_dir("review-replan-before-gate"),
            {
                "status": "pass",
                "human_feedback_before_review": True,
                "human_feedback_action_allowlist": {"before_review": ["replan"]},
            },
        ),
        (
            _example_config_dir("parallel-do-work"),
            {
                "status": "pass",
                "do_work_parallel_enabled": True,
                "do_work_parallel_planner_enabled": False,
                "do_work_replan_enabled": False,
            },
        ),
        (
            _example_config_dir("parallel-planner"),
            {
                "status": "pass",
                "do_work_parallel_enabled": True,
                "do_work_parallel_planner_enabled": True,
                "do_work_replan_enabled": False,
            },
        ),
        (
            _example_config_dir("parallel-replan"),
            {
                "status": "pass",
                "do_work_parallel_enabled": True,
                "do_work_parallel_planner_enabled": True,
                "do_work_replan_enabled": True,
            },
        ),
    ],
)
def test_operator_playbook_doctor_examples_route_through_cli(
    monkeypatch: pytest.MonkeyPatch,
    config_dir: Path,
    expected: dict[str, object],
):
    from crewai_headless_flow import cli

    _patch_clean_doctor_environment(monkeypatch)
    captured: dict[str, object] = {}

    def fake_print_report(report, output_format: str) -> None:
        captured["report"] = report
        captured["format"] = output_format

    monkeypatch.setattr(cli, "_print_report", fake_print_report)

    rc = cli.main(
        [
            "doctor",
            "--config-dir",
            str(config_dir),
        ]
    )

    report = cast(Any, captured["report"])
    data = report.to_dict()
    stages = {stage["stage"]: stage for stage in data["resolved_runtime"]["stages"]}

    assert rc == 0
    assert captured["format"] == "text"
    assert report.status == expected["status"]
    if "do_work_worker" in expected:
        assert stages["do_work"]["worker"] == expected["do_work_worker"]
    if "do_work_model" in expected:
        assert stages["do_work"]["model"] == expected["do_work_model"]
    if "do_work_crew_enabled" in expected:
        assert (
            stages["do_work"]["runtime_knobs"]["crew"]["enabled"]
            is expected["do_work_crew_enabled"]
        )
    if "plan_crew_enabled" in expected:
        assert (
            stages["plan"]["runtime_knobs"]["crew"]["enabled"]
            is expected["plan_crew_enabled"]
        )
    if "do_work_parallel_enabled" in expected:
        assert (
            stages["do_work"]["runtime_knobs"]["parallel"]["enabled"]
            is expected["do_work_parallel_enabled"]
        )
    if "do_work_parallel_planner_enabled" in expected:
        assert (
            stages["do_work"]["runtime_knobs"]["parallel"]["planner"]["enabled"]
            is expected["do_work_parallel_planner_enabled"]
        )
    if "do_work_replan_enabled" in expected:
        assert (
            stages["do_work"]["runtime_knobs"]["replan"]["enabled"]
            is expected["do_work_replan_enabled"]
        )
    if "review_crew_enabled" in expected:
        assert (
            stages["review"]["runtime_knobs"]["crew"]["enabled"]
            is expected["review_crew_enabled"]
        )
    if "human_feedback_enabled" in expected:
        assert (
            data["resolved_runtime"]["human_feedback"]["enabled"]
            is expected["human_feedback_enabled"]
        )
    if "human_feedback_before_plan" in expected:
        assert (
            data["resolved_runtime"]["human_feedback"]["before_plan"]
            is expected["human_feedback_before_plan"]
        )
    if "human_feedback_capture_instructions" in expected:
        assert (
            data["resolved_runtime"]["human_feedback"]["capture_instructions"]
            is expected["human_feedback_capture_instructions"]
        )
    if "human_feedback_before_do_work" in expected:
        assert (
            data["resolved_runtime"]["human_feedback"]["before_do_work"]
            is expected["human_feedback_before_do_work"]
        )
    if "human_feedback_after_review" in expected:
        assert (
            data["resolved_runtime"]["human_feedback"]["after_review"]
            is expected["human_feedback_after_review"]
        )
    if "human_feedback_before_review" in expected:
        assert (
            data["resolved_runtime"]["human_feedback"]["before_review"]
            is expected["human_feedback_before_review"]
        )
    if "human_feedback_action_allowlist" in expected:
        assert (
            data["resolved_runtime"]["human_feedback"]["action_allowlist"]
            == expected["human_feedback_action_allowlist"]
        )
    if "requires_check" in expected:
        assert any(check.name == expected["requires_check"] for check in report.checks)


@pytest.mark.parametrize(
    ("config_dir", "extra_args", "expected"),
    [
        (
            REPO_ROOT / "config",
            [],
            {
                "do_work_worker": "grok",
                "human_feedback_enabled": False,
            },
        ),
        (
            _example_config_dir("claude-do-work"),
            [],
            {
                "do_work_worker": "claude",
                "do_work_model": "sonnet",
                "human_feedback_enabled": False,
            },
        ),
        (
            _example_config_dir("gemini-do-work"),
            [],
            {
                "do_work_worker": "gemini",
                "do_work_model": "gemini-2.5-pro",
                "human_feedback_enabled": False,
            },
        ),
        (
            _example_config_dir("implementation-crew"),
            ["--debug-report-file", "{debug_report}"],
            {
                "do_work_worker": "grok",
                "do_work_crew_enabled": True,
                "do_work_decomposition_enabled": True,
                "writes_debug_report": True,
            },
        ),
        (
            _example_config_dir("implementation-crew-parallel"),
            ["--debug-report-file", "{debug_report}"],
            {
                "do_work_worker": "grok",
                "do_work_crew_enabled": True,
                "do_work_decomposition_enabled": True,
                "do_work_parallel_enabled": True,
                "do_work_parallel_planner_enabled": False,
                "do_work_replan_enabled": False,
                "writes_debug_report": True,
            },
        ),
        (
            _example_config_dir("implementation-crew-parallel-planner"),
            ["--debug-report-file", "{debug_report}"],
            {
                "do_work_worker": "grok",
                "do_work_crew_enabled": True,
                "do_work_decomposition_enabled": True,
                "do_work_parallel_enabled": True,
                "do_work_parallel_planner_enabled": True,
                "do_work_replan_enabled": False,
                "writes_debug_report": True,
            },
        ),
        (
            _example_config_dir("implementation-crew-parallel-replan"),
            ["--debug-report-file", "{debug_report}"],
            {
                "do_work_worker": "grok",
                "do_work_crew_enabled": True,
                "do_work_decomposition_enabled": True,
                "do_work_parallel_enabled": True,
                "do_work_parallel_planner_enabled": True,
                "do_work_replan_enabled": True,
                "writes_debug_report": True,
            },
        ),
        (
            _example_config_dir("plan-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_before_plan": True,
                "human_feedback_capture_instructions": True,
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("planning-crew"),
            ["--debug-report-file", "{debug_report}"],
            {
                "do_work_worker": "grok",
                "plan_crew_enabled": True,
                "writes_debug_report": True,
            },
        ),
        (
            _example_config_dir("review-crew"),
            ["--debug-report-file", "{debug_report}"],
            {
                "do_work_worker": "grok",
                "review_crew_enabled": True,
                "writes_debug_report": True,
            },
        ),
        (
            _example_config_dir("operator-review-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_after_review": True,
                "human_feedback_action_allowlist": {
                    "after_review": ["rerun-review", "force-revise", "force-pass"]
                },
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("review-rerun-review-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_after_review": True,
                "human_feedback_action_allowlist": {"after_review": ["rerun-review"]},
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("review-targeting-only-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_after_review": True,
                "human_feedback_action_allowlist": {"after_review": ["target-tasks"]},
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("review-replan-only-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_after_review": True,
                "human_feedback_action_allowlist": {"after_review": ["replan"]},
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("review-force-revise-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_after_review": True,
                "human_feedback_action_allowlist": {"after_review": ["force-revise"]},
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("review-force-pass-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_after_review": True,
                "human_feedback_action_allowlist": {"after_review": ["force-pass"]},
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("review-targeting-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_after_review": True,
                "human_feedback_action_allowlist": {
                    "after_review": [
                        "rerun-review",
                        "target-tasks",
                        "force-revise",
                        "force-pass",
                    ]
                },
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("review-targeting-before-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_before_review": True,
                "human_feedback_after_review": False,
                "human_feedback_action_allowlist": {"before_review": ["target-tasks"]},
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("review-force-revise-before-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_before_review": True,
                "human_feedback_after_review": False,
                "human_feedback_action_allowlist": {"before_review": ["force-revise"]},
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("review-force-pass-before-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_before_review": True,
                "human_feedback_after_review": False,
                "human_feedback_action_allowlist": {"before_review": ["force-pass"]},
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("review-replan-before-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_before_review": True,
                "human_feedback_after_review": False,
                "human_feedback_action_allowlist": {"before_review": ["replan"]},
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("review-replan-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_after_review": True,
                "human_feedback_action_allowlist": {
                    "after_review": [
                        "replan",
                        "rerun-review",
                        "force-revise",
                        "force-pass",
                    ]
                },
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("parallel-do-work"),
            ["--debug-report-file", "{debug_report}"],
            {
                "do_work_parallel_enabled": True,
                "do_work_parallel_planner_enabled": False,
                "do_work_replan_enabled": False,
                "writes_debug_report": True,
            },
        ),
        (
            _example_config_dir("parallel-planner"),
            ["--debug-report-file", "{debug_report}"],
            {
                "do_work_parallel_enabled": True,
                "do_work_parallel_planner_enabled": True,
                "do_work_replan_enabled": False,
                "writes_debug_report": True,
            },
        ),
        (
            _example_config_dir("guided-operator-loop"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_before_do_work": True,
                "human_feedback_after_review": True,
                "human_feedback_before_finalize": True,
                "human_feedback_capture_instructions": True,
                "human_feedback_action_allowlist": {
                    "do_work": ["replan", "skip-to-review", "target-tasks"],
                    "after_review": [
                        "replan",
                        "rerun-review",
                        "target-tasks",
                        "force-revise",
                        "force-pass",
                    ],
                    "finalize": [
                        "skip-finalize",
                        "rerun-review",
                        "force-revise",
                        "replan",
                        "target-tasks",
                    ],
                },
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("do-work-targeting-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_before_do_work": True,
                "human_feedback_after_review": False,
                "human_feedback_action_allowlist": {"before_do_work": ["target-tasks"]},
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("do-work-replan-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_before_do_work": True,
                "human_feedback_after_review": False,
                "human_feedback_action_allowlist": {"before_do_work": ["replan"]},
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("do-work-skip-to-review-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_before_do_work": True,
                "human_feedback_after_review": False,
                "human_feedback_action_allowlist": {
                    "before_do_work": ["skip-to-review"]
                },
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("finalize-review-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_before_finalize": True,
                "human_feedback_action_allowlist": {
                    "before_finalize": [
                        "rerun-review",
                        "force-revise",
                        "replan",
                        "target-tasks",
                        "skip-finalize",
                    ]
                },
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("finalize-rerun-review-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_before_finalize": True,
                "human_feedback_action_allowlist": {
                    "before_finalize": ["rerun-review"]
                },
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("finalize-replan-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_before_finalize": True,
                "human_feedback_action_allowlist": {"before_finalize": ["replan"]},
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("finalize-force-revise-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_before_finalize": True,
                "human_feedback_action_allowlist": {
                    "before_finalize": ["force-revise"]
                },
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("finalize-skip-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_before_finalize": True,
                "human_feedback_action_allowlist": {
                    "before_finalize": ["skip-finalize"]
                },
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("finalize-targeting-gate"),
            ["--state-file", "{state_file}"],
            {
                "human_feedback_enabled": True,
                "human_feedback_before_finalize": True,
                "human_feedback_action_allowlist": {
                    "before_finalize": ["target-tasks"]
                },
                "writes_state_file": True,
            },
        ),
        (
            _example_config_dir("parallel-replan"),
            ["--debug-report-file", "{debug_report}"],
            {
                "do_work_parallel_enabled": True,
                "do_work_parallel_planner_enabled": True,
                "do_work_replan_enabled": True,
                "writes_debug_report": True,
            },
        ),
    ],
)
def test_operator_playbook_run_examples_route_through_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_dir: Path,
    extra_args: list[str],
    expected: dict[str, object],
):
    from crewai_headless_flow import cli

    calls: list[dict] = []
    state_file = tmp_path / "playbook-state.json"
    debug_report_file = tmp_path / "playbook-report.md"

    def fake_run_headless_flow(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status="completed",
            model_dump=lambda: {
                "status": "completed",
                "debug_report": "# Flow Execution Report\nplaybook smoke",
            },
        )

    monkeypatch.setattr(cli, "run_headless_flow", fake_run_headless_flow)

    resolved_extra_args = [
        arg.format(
            state_file=state_file,
            debug_report=debug_report_file,
        )
        for arg in extra_args
    ]

    rc = cli.main(
        [
            "run",
            "--request",
            "Add subtract and divide helpers plus tests and update README usage notes",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(config_dir),
            *resolved_extra_args,
        ]
    )

    cfg = calls[0]["config"]
    plan = cfg.get_stage("plan")
    do_work = cfg.get_stage("do_work")
    review = cfg.get_stage("review")

    assert rc == 0
    if "do_work_worker" in expected:
        assert do_work.worker == expected["do_work_worker"]
    if "do_work_model" in expected:
        assert do_work.model == expected["do_work_model"]
    if "do_work_crew_enabled" in expected:
        assert do_work.extra["crew"]["enabled"] is expected["do_work_crew_enabled"]
    if "plan_crew_enabled" in expected:
        assert plan.extra["crew"]["enabled"] is expected["plan_crew_enabled"]
    if "do_work_decomposition_enabled" in expected:
        assert (
            do_work.extra["crew"]["decomposition"]["enabled"]
            is expected["do_work_decomposition_enabled"]
        )
    if "do_work_parallel_enabled" in expected:
        assert (
            do_work.extra["parallel"]["enabled"] is expected["do_work_parallel_enabled"]
        )
    if "do_work_parallel_planner_enabled" in expected:
        assert (
            do_work.extra["parallel"]["planner"]["enabled"]
            is expected["do_work_parallel_planner_enabled"]
        )
    if "do_work_replan_enabled" in expected:
        assert do_work.extra["replan"]["enabled"] is expected["do_work_replan_enabled"]
    if "review_crew_enabled" in expected:
        assert review.extra["crew"]["enabled"] is expected["review_crew_enabled"]
    if "human_feedback_enabled" in expected:
        assert cfg.human_feedback["enabled"] is expected["human_feedback_enabled"]
    if "human_feedback_before_plan" in expected:
        assert (
            cfg.human_feedback["before_plan"] is expected["human_feedback_before_plan"]
        )
    if "human_feedback_before_do_work" in expected:
        assert (
            cfg.human_feedback["before_do_work"]
            is expected["human_feedback_before_do_work"]
        )
    if "human_feedback_before_review" in expected:
        assert (
            cfg.human_feedback["before_review"]
            is expected["human_feedback_before_review"]
        )
    if "human_feedback_after_review" in expected:
        assert (
            cfg.human_feedback["after_review"]
            is expected["human_feedback_after_review"]
        )
    if "human_feedback_before_finalize" in expected:
        assert (
            cfg.human_feedback["before_finalize"]
            is expected["human_feedback_before_finalize"]
        )
    if "human_feedback_capture_instructions" in expected:
        assert (
            cfg.human_feedback["capture_instructions"]
            is expected["human_feedback_capture_instructions"]
        )
    if "human_feedback_action_allowlist" in expected:
        assert (
            cfg.human_feedback["action_allowlist"]
            == expected["human_feedback_action_allowlist"]
        )
    if expected.get("writes_state_file"):
        assert state_file.exists()
        assert json.loads(state_file.read_text())["status"] == "completed"
    if expected.get("writes_debug_report"):
        assert (
            debug_report_file.read_text() == "# Flow Execution Report\nplaybook smoke"
        )


def test_operator_playbook_resume_example_routes_example_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from crewai_headless_flow import cli

    state_path = tmp_path / "review-gate-state.json"
    state_path.write_text(
        json.dumps(
            {
                "request": "ship cli",
                "target_repo": str(tmp_path),
                "status": "aborted_by_human",
                "aborted_stage": "review",
                "aborted_stage_input": "work summary",
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
            str(_example_config_dir("operator-review-gate")),
        ]
    )

    cfg = calls[0]["config"]

    assert rc == 0
    assert calls[0]["state"].aborted_stage == "review"
    assert cfg.human_feedback["after_review"] is True
    assert cfg.human_feedback["action_allowlist"] == {
        "after_review": ["rerun-review", "force-revise", "force-pass"]
    }


def test_readme_doctor_override_example_routes_through_cli(
    monkeypatch: pytest.MonkeyPatch,
):
    from crewai_headless_flow import cli

    _patch_clean_doctor_environment(monkeypatch)
    captured: dict[str, object] = {}

    def fake_print_report(report, output_format: str) -> None:
        captured["report"] = report
        captured["format"] = output_format

    monkeypatch.setattr(cli, "_print_report", fake_print_report)

    rc = cli.main(
        [
            "doctor",
            "--config-dir",
            str(REPO_ROOT / "config"),
            "--override-worker",
            "do_work=claude",
            "--override-stage-extra",
            "review.crew.enabled=true",
        ]
    )

    report = cast(Any, captured["report"])
    data = report.to_dict()
    stages = {stage["stage"]: stage for stage in data["resolved_runtime"]["stages"]}

    assert rc == 0
    assert captured["format"] == "text"
    assert report.status == "warn"
    assert stages["do_work"]["worker"] == "claude"
    assert stages["review"]["runtime_knobs"]["crew"]["enabled"] is True
    assert any(check.name == "config.review_crew" for check in report.checks)
    assert any(check.name == "cli.ollama" for check in report.checks)


def test_operator_playbook_preflight_example_routes_through_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from crewai_headless_flow import cli

    target = tmp_path / "demo-target"
    target.mkdir()
    (target / "pyproject.toml").write_text("[project]\nname='demo-target'\n")
    (target / "README.md").write_text("# Demo Target\n")

    _patch_clean_preflight_git(monkeypatch)
    captured: dict[str, object] = {}

    def fake_print_report(report, output_format: str) -> None:
        captured["report"] = report
        captured["format"] = output_format

    monkeypatch.setattr(cli, "_print_report", fake_print_report)

    rc = cli.main(
        [
            "preflight",
            "--target-repo",
            str(target),
        ]
    )

    report = cast(Any, captured["report"])

    assert rc == 0
    assert captured["format"] == "text"
    assert report.status == "pass"
    assert report.target_repo == str(target.resolve())
    assert report.tooling["pyproject.toml"] is True
    assert report.tooling["README.md"] is True
    assert report.git["is_git_repo"] is True
    assert report.git["is_dirty"] is False


def test_operator_playbook_override_example_applies_documented_runtime_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
            "...",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(_example_config_dir("operator-review-gate")),
            "--override-human-feedback-action",
            "do_work=replan,skip-to-review,target-tasks",
            "--override-human-feedback-action",
            "after_review=replan,rerun-review,target-tasks,force-revise,force-pass",
            "--override-default-worker",
            "claude",
            "--override-skill",
            "do_work=test-driven-development",
            "--override-worker",
            "do_work=claude",
        ]
    )

    cfg = calls[0]["config"]
    do_work = cfg.get_stage("do_work")

    assert rc == 0
    assert cfg.defaults["worker"] == "claude"
    assert do_work.skill == "test-driven-development"
    assert do_work.worker == "claude"
    assert cfg.human_feedback["action_allowlist"] == {
        "do_work": ["replan", "skip-to-review", "target-tasks"],
        "after_review": [
            "replan",
            "rerun-review",
            "target-tasks",
            "force-revise",
            "force-pass",
        ],
    }


@pytest.mark.parametrize(
    ("extra_args", "expected"),
    [
        (
            [
                "--override-stage-extra",
                "do_work.crew.enabled=true",
                "--override-stage-extra",
                "do_work.crew.decomposition.enabled=true",
                "--override-stage-extra",
                "do_work.crew.decomposition.max_subtasks=3",
            ],
            {
                "do_work_crew_enabled": True,
                "do_work_decomposition_enabled": True,
                "do_work_decomposition_max_subtasks": 3,
                "do_work_parallel_enabled": False,
                "do_work_parallel_planner_enabled": False,
                "do_work_replan_enabled": False,
                "do_work_replan_max_execution_replans": 1,
            },
        ),
        (
            [
                "--override-stage-extra",
                "do_work.crew.enabled=true",
                "--override-stage-extra",
                "do_work.crew.decomposition.enabled=true",
                "--override-stage-extra",
                "do_work.crew.decomposition.max_subtasks=3",
                "--override-stage-extra",
                "do_work.parallel.enabled=true",
                "--override-stage-extra",
                "do_work.parallel.max_workers=4",
            ],
            {
                "do_work_crew_enabled": True,
                "do_work_decomposition_enabled": True,
                "do_work_decomposition_max_subtasks": 3,
                "do_work_parallel_enabled": True,
                "do_work_parallel_max_workers": 4,
                "do_work_parallel_planner_enabled": False,
                "do_work_replan_enabled": False,
                "do_work_replan_max_execution_replans": 1,
            },
        ),
        (
            [
                "--override-stage-extra",
                "do_work.crew.enabled=true",
                "--override-stage-extra",
                "do_work.crew.decomposition.enabled=true",
                "--override-stage-extra",
                "do_work.crew.decomposition.max_subtasks=3",
                "--override-stage-extra",
                "do_work.parallel.enabled=true",
                "--override-stage-extra",
                "do_work.parallel.max_workers=4",
                "--override-stage-extra",
                "do_work.parallel.planner.enabled=true",
            ],
            {
                "do_work_crew_enabled": True,
                "do_work_decomposition_enabled": True,
                "do_work_decomposition_max_subtasks": 3,
                "do_work_parallel_enabled": True,
                "do_work_parallel_max_workers": 4,
                "do_work_parallel_planner_enabled": True,
                "do_work_replan_enabled": False,
                "do_work_replan_max_execution_replans": 1,
            },
        ),
        (
            [
                "--override-stage-extra",
                "do_work.crew.enabled=true",
                "--override-stage-extra",
                "do_work.crew.decomposition.enabled=true",
                "--override-stage-extra",
                "do_work.crew.decomposition.max_subtasks=3",
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
            ],
            {
                "do_work_crew_enabled": True,
                "do_work_decomposition_enabled": True,
                "do_work_decomposition_max_subtasks": 3,
                "do_work_parallel_enabled": True,
                "do_work_parallel_max_workers": 4,
                "do_work_parallel_planner_enabled": True,
                "do_work_replan_enabled": True,
                "do_work_replan_on_execution_failure": True,
                "do_work_replan_on_cross_task_change": True,
                "do_work_replan_on_ambiguous_success": True,
                "do_work_replan_max_execution_replans": 2,
            },
        ),
    ],
)
def test_operator_playbook_override_recipes_match_documented_implementation_crew_lanes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_args: list[str],
    expected: dict[str, object],
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
            "...",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(REPO_ROOT / "config"),
            *extra_args,
        ]
    )

    cfg = calls[0]["config"]
    do_work = cfg.get_stage("do_work")

    assert rc == 0
    assert do_work.extra["crew"]["enabled"] is expected["do_work_crew_enabled"]
    assert (
        do_work.extra["crew"]["decomposition"]["enabled"]
        is expected["do_work_decomposition_enabled"]
    )
    assert (
        do_work.extra["crew"]["decomposition"]["max_subtasks"]
        == expected["do_work_decomposition_max_subtasks"]
    )
    assert do_work.extra["parallel"]["enabled"] is expected["do_work_parallel_enabled"]
    if "do_work_parallel_max_workers" in expected:
        assert (
            do_work.extra["parallel"]["max_workers"]
            == expected["do_work_parallel_max_workers"]
        )
    assert (
        do_work.extra["parallel"]["planner"]["enabled"]
        is expected["do_work_parallel_planner_enabled"]
    )
    assert do_work.extra["replan"]["enabled"] is expected["do_work_replan_enabled"]
    if "do_work_replan_on_execution_failure" in expected:
        assert (
            do_work.extra["replan"]["on_execution_failure"]
            is expected["do_work_replan_on_execution_failure"]
        )
    if "do_work_replan_on_cross_task_change" in expected:
        assert (
            do_work.extra["replan"]["on_cross_task_change"]
            is expected["do_work_replan_on_cross_task_change"]
        )
    if "do_work_replan_on_ambiguous_success" in expected:
        assert (
            do_work.extra["replan"]["on_ambiguous_success"]
            is expected["do_work_replan_on_ambiguous_success"]
        )
    assert (
        do_work.extra["replan"]["max_execution_replans"]
        == expected["do_work_replan_max_execution_replans"]
    )


@pytest.mark.parametrize(
    ("extra_args", "expected"),
    [
        (
            [
                "--override-stage-extra",
                "do_work.parallel.enabled=true",
                "--override-stage-extra",
                "do_work.parallel.max_workers=4",
            ],
            {
                "do_work_crew_enabled": False,
                "do_work_parallel_enabled": True,
                "do_work_parallel_max_workers": 4,
                "do_work_parallel_planner_enabled": False,
                "do_work_replan_enabled": False,
                "do_work_replan_max_execution_replans": 1,
            },
        ),
        (
            [
                "--override-stage-extra",
                "do_work.parallel.enabled=true",
                "--override-stage-extra",
                "do_work.parallel.max_workers=4",
                "--override-stage-extra",
                "do_work.parallel.planner.enabled=true",
            ],
            {
                "do_work_crew_enabled": False,
                "do_work_parallel_enabled": True,
                "do_work_parallel_max_workers": 4,
                "do_work_parallel_planner_enabled": True,
                "do_work_replan_enabled": False,
                "do_work_replan_max_execution_replans": 1,
            },
        ),
        (
            [
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
            ],
            {
                "do_work_crew_enabled": False,
                "do_work_parallel_enabled": True,
                "do_work_parallel_max_workers": 4,
                "do_work_parallel_planner_enabled": True,
                "do_work_replan_enabled": True,
                "do_work_replan_on_execution_failure": True,
                "do_work_replan_on_cross_task_change": True,
                "do_work_replan_on_ambiguous_success": True,
                "do_work_replan_max_execution_replans": 2,
            },
        ),
    ],
)
def test_operator_playbook_override_recipes_match_documented_parallel_lanes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_args: list[str],
    expected: dict[str, object],
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
            "...",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(REPO_ROOT / "config"),
            *extra_args,
        ]
    )

    cfg = calls[0]["config"]
    do_work = cfg.get_stage("do_work")

    assert rc == 0
    assert do_work.extra["crew"]["enabled"] is expected["do_work_crew_enabled"]
    assert do_work.extra["parallel"]["enabled"] is expected["do_work_parallel_enabled"]
    assert (
        do_work.extra["parallel"]["max_workers"]
        == expected["do_work_parallel_max_workers"]
    )
    assert (
        do_work.extra["parallel"]["planner"]["enabled"]
        is expected["do_work_parallel_planner_enabled"]
    )
    assert do_work.extra["replan"]["enabled"] is expected["do_work_replan_enabled"]
    if "do_work_replan_on_execution_failure" in expected:
        assert (
            do_work.extra["replan"]["on_execution_failure"]
            is expected["do_work_replan_on_execution_failure"]
        )
    if "do_work_replan_on_cross_task_change" in expected:
        assert (
            do_work.extra["replan"]["on_cross_task_change"]
            is expected["do_work_replan_on_cross_task_change"]
        )
    if "do_work_replan_on_ambiguous_success" in expected:
        assert (
            do_work.extra["replan"]["on_ambiguous_success"]
            is expected["do_work_replan_on_ambiguous_success"]
        )
    assert (
        do_work.extra["replan"]["max_execution_replans"]
        == expected["do_work_replan_max_execution_replans"]
    )


@pytest.mark.parametrize(
    ("extra_args", "expected"),
    [
        (
            [
                "--override-worker",
                "do_work=claude",
                "--override-model",
                "do_work=sonnet",
            ],
            {
                "do_work_worker": "claude",
                "do_work_model": "sonnet",
            },
        ),
        (
            [
                "--override-worker",
                "do_work=gemini",
                "--override-model",
                "do_work=gemini-2.5-pro",
            ],
            {
                "do_work_worker": "gemini",
                "do_work_model": "gemini-2.5-pro",
            },
        ),
        (
            [
                "--override-stage-extra",
                "plan.crew.enabled=true",
            ],
            {
                "plan_crew_enabled": True,
            },
        ),
        (
            [
                "--override-stage-extra",
                "review.crew.enabled=true",
            ],
            {
                "review_crew_enabled": True,
            },
        ),
    ],
)
def test_operator_playbook_override_recipes_match_documented_simple_lanes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_args: list[str],
    expected: dict[str, object],
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
            "...",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(REPO_ROOT / "config"),
            *extra_args,
        ]
    )

    cfg = calls[0]["config"]
    plan = cfg.get_stage("plan")
    do_work = cfg.get_stage("do_work")
    review = cfg.get_stage("review")

    assert rc == 0
    if "do_work_worker" in expected:
        assert do_work.worker == expected["do_work_worker"]
    if "do_work_model" in expected:
        assert do_work.model == expected["do_work_model"]
    if "plan_crew_enabled" in expected:
        assert plan.extra["crew"]["enabled"] is expected["plan_crew_enabled"]
    if "review_crew_enabled" in expected:
        assert review.extra["crew"]["enabled"] is expected["review_crew_enabled"]


def test_operator_playbook_override_example_can_clear_inherited_gate_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
            "...",
            "--target-repo",
            str(tmp_path),
            "--config-dir",
            str(_example_config_dir("review-targeting-gate")),
            "--override-human-feedback-action",
            "after_review=none",
        ]
    )

    cfg = calls[0]["config"]

    assert rc == 0
    assert cfg.human_feedback["after_review"] is True
    assert cfg.human_feedback["action_allowlist"] == {
        "after_review": [],
    }
