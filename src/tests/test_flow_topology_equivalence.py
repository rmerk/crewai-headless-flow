"""Phase 2: declarative topology twin load/resolve + kickoff equivalence."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from crewai_headless_flow.config import FlowConfig
from crewai_headless_flow.flow import CrewAIHeadlessFlow
from crewai_headless_flow.flow_topology import (
    build_topology_twin_flow,
    load_flow_definition,
    resolve_flow_yaml_path,
)
from crewai_headless_flow.state import FlowState
from crewai_headless_flow.workers.base import CoderResult
from tests.test_flow_definition_projection import EXPECTED_METHODS

pytestmark = pytest.mark.offline

STAGE_REFS = {
    "plan": "crewai_headless_flow.stages.plan:execute_plan",
    "do_work": "crewai_headless_flow.stages.do_work:execute_do_work",
    "review": "crewai_headless_flow.stages.review:execute_review",
    "process_revision": (
        "crewai_headless_flow.stages.revision:execute_process_revision"
    ),
    "finalize": "crewai_headless_flow.stages.finalize:execute_finalize",
    "handle_aborted": ("crewai_headless_flow.stages.terminal:execute_handle_aborted"),
    "handle_failed": ("crewai_headless_flow.stages.terminal:execute_handle_failed"),
}

PLAN_JSON = json.dumps(
    {
        "spec": "Add a hello marker file.",
        "tasks": [
            {
                "id": 1,
                "title": "Add hello",
                "description": "Create hello.txt",
                "acceptance_criteria": ["hello.txt exists"],
                "verification": ["test -f hello.txt"],
                "dependencies": [],
                "files": ["hello.txt"],
                "estimated_scope": "S",
            }
        ],
    }
)


class PlanWorker:
    def run(self, **kwargs: Any) -> CoderResult:
        return CoderResult(summary=PLAN_JSON, raw_output=PLAN_JSON, exit_code=0)


class EditWorker:
    def run(
        self, task: str, cwd: str, mode: str = "edit", **kwargs: Any
    ) -> CoderResult:
        Path(cwd, "hello.txt").write_text("hi\n", encoding="utf-8")
        return CoderResult(
            summary="wrote hello",
            changed_files=["hello.txt"],
            raw_output="ok",
            exit_code=0,
        )


class ReviewSequenceWorker:
    def __init__(self, outcomes: list[str]):
        self.outcomes = list(outcomes)
        self.calls = 0

    def run(
        self, task: str, cwd: str, mode: str = "edit", **kwargs: Any
    ) -> CoderResult:
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "pass"
        if outcome == "pass":
            raw = '{"status": "pass", "issues": [], "summary": "Looks good"}'
        else:
            raw = (
                '{"status": "revise", "issues": ["Missing tests"], '
                '"summary": "Needs work"}'
            )
        return CoderResult(summary="review", raw_output=raw, exit_code=0)


class FinalizeWorker:
    def run(self, **kwargs: Any) -> CoderResult:
        return CoderResult(
            summary="docs", changed_files=[], raw_output="docs", exit_code=0
        )


class FakeVerifyRunner:
    def __init__(self, exit_code: int = 1):
        self.exit_code = exit_code
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: list[str],
        *,
        cwd: str,
        capture_output: bool,
        text: bool,
        timeout: float | None,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.exit_code, "2 failed", "")


class _AbortDecision:
    proceed = False
    action = "abort"
    instructions = None
    task_ids = None


def _base_config(**extra: Any) -> FlowConfig:
    kwargs: dict[str, Any] = {
        "skills": {
            "plan": "planning-and-task-breakdown",
            "do_work": "incremental-implementation",
            "review": "code-review-and-quality",
            "finalize": "documentation-and-adrs",
        },
        "workers": {
            "plan": {"worker": "codex"},
            "do_work": {"worker": "codex"},
            "review": {"worker": "codex"},
            "finalize": {"worker": "codex"},
        },
        "defaults": {"worker": "codex", "timeout": 300},
        "human_feedback": {"enabled": False},
        "deliver": {"enabled": False},
    }
    kwargs.update(extra)
    return FlowConfig(**kwargs)


def _terminal_snapshot(
    flow: CrewAIHeadlessFlow, *, review_calls: int
) -> dict[str, Any]:
    return {
        "status": flow.state.status,
        "review_status": flow.state.review_status,
        "revisions": flow.state.revisions,
        "last_stage": flow.state.last_stage,
        "task_statuses": [(task.id, task.status) for task in flow.state.tasks],
        "verification_passed": [run.passed for run in flow.state.verification_runs],
        "review_calls": review_calls,
        "errors": list(flow.state.errors),
        "issues_prefix": [issue[:48] for issue in flow.state.issues[:3]],
    }


def _prepare_flow(
    factory: Callable[..., CrewAIHeadlessFlow],
    *,
    config: FlowConfig,
    target_repo: Path,
    review_outcomes: list[str],
    verification_runner: FakeVerifyRunner | None = None,
    abort_before_plan: bool = False,
) -> tuple[CrewAIHeadlessFlow, ReviewSequenceWorker]:
    flow = factory(config=config)
    flow.suppress_flow_events = True
    flow.max_method_calls = 200
    flow.config.print_mapping = lambda: None  # type: ignore[method-assign]
    review = ReviewSequenceWorker(review_outcomes)
    flow._workers["plan"] = PlanWorker()  # type: ignore[assignment]
    flow._workers["do_work"] = EditWorker()  # type: ignore[assignment]
    flow._workers["review"] = review  # type: ignore[assignment]
    flow._workers["finalize"] = FinalizeWorker()  # type: ignore[assignment]
    if verification_runner is not None:
        flow._verification_runner = verification_runner
    if abort_before_plan:
        flow._maybe_ask_human = lambda *args, **kwargs: _AbortDecision()  # type: ignore[method-assign]
    return flow, review


def _kickoff_snapshot(
    factory: Callable[..., CrewAIHeadlessFlow],
    *,
    tmp_path: Path,
    config: FlowConfig,
    review_outcomes: list[str],
    max_revisions: int = 2,
    verification_runner: FakeVerifyRunner | None = None,
    abort_before_plan: bool = False,
    continue_revise_cycle: bool = False,
) -> dict[str, Any]:
    target = tmp_path / "repo"
    target.mkdir(parents=True)
    (target / "README.md").write_text("demo\n", encoding="utf-8")
    flow, review = _prepare_flow(
        factory,
        config=config,
        target_repo=target,
        review_outcomes=review_outcomes,
        verification_runner=verification_runner,
        abort_before_plan=abort_before_plan,
    )
    inputs = FlowState(
        request="add hello",
        target_repo=str(target),
        max_revisions=max_revisions,
    ).model_dump()
    flow.kickoff(inputs=inputs)

    # CrewAI 1.15.2 marks multi-event or_() listeners as fired after the first
    # plan→do_work edge, so process_revision does not automatically re-enter
    # do_work. Continue one revise cycle via public methods when requested so
    # plan→revise→pass can still reach a shared terminal state.
    if (
        continue_revise_cycle
        and flow.state.status == "running"
        and flow.state.revisions >= 1
    ):
        flow._clear_or_listeners()
        work = flow.do_work(flow.state.latest_work_summary or "revision")
        decision = flow.review(work)
        if decision == "pass":
            flow.finalize(decision)

    return _terminal_snapshot(flow, review_calls=review.calls)


def test_resolve_flow_yaml_path_defaults_to_config_pack():
    path = resolve_flow_yaml_path()
    assert path.is_file()
    assert path.name == "flow.yaml"


def test_load_flow_definition_matches_canonical_topology_and_stage_refs():
    definition = load_flow_definition()

    assert definition.name == "CrewAIHeadlessFlow"
    assert set(definition.methods) == set(EXPECTED_METHODS)

    for name, expected in EXPECTED_METHODS.items():
        method = definition.methods[name]
        assert method.start == expected["start"], name
        assert method.listen == expected["listen"], name
        assert method.router is expected["router"], name
        assert method.emit == expected["emit"], name
        assert method.do.call == "code", name
        assert method.do.ref == STAGE_REFS[name], name


def test_load_flow_definition_fails_closed_on_incomplete_graph(tmp_path: Path):
    incomplete = {
        "schema": "crewai.flow/v1",
        "name": "Incomplete",
        "state": {"type": "dict", "default": {}},
        "methods": {
            "plan": {
                "start": True,
                "do": {
                    "call": "code",
                    "ref": "crewai_headless_flow.stages.plan:execute_plan",
                },
            }
        },
    }
    (tmp_path / "flow.yaml").write_text(yaml.safe_dump(incomplete), encoding="utf-8")

    with pytest.raises(ValueError, match="incomplete|missing"):
        load_flow_definition(config_dir=tmp_path)


def test_resolve_flow_yaml_path_falls_back_when_config_dir_missing_flow_yaml(
    tmp_path: Path,
):
    # Missing twin under an override pack falls back to the default pack
    # (ADR-0012); present-but-invalid files fail closed in load_flow_definition.
    path = resolve_flow_yaml_path(config_dir=tmp_path)
    assert path.is_file()
    assert path == resolve_flow_yaml_path()


def test_build_topology_twin_flow_binds_stage_callables():
    twin = build_topology_twin_flow()

    assert twin.__class__.__name__ == "CrewAIHeadlessFlow"
    assert set(twin._methods) == set(STAGE_REFS)
    for name, expected_ref in STAGE_REFS.items():
        assert callable(getattr(twin, name)), name
        # Twin definition (and therefore CodeAction refs) point at stage bodies,
        # not CrewAIHeadlessFlow.<method> class wrappers.
        assert twin._definition.methods[name].do.ref == expected_ref, name
        assert twin._definition.methods[name].do.call == "code", name
        action = twin._methods[name].__closure__[0].cell_contents
        assert action.definition.ref == expected_ref, name
        assert action.definition.call == "code", name


def test_class_vs_twin_kickoff_plan_to_pass(tmp_path: Path):
    cfg = _base_config()
    class_snap = _kickoff_snapshot(
        CrewAIHeadlessFlow,
        tmp_path=tmp_path / "class",
        config=cfg,
        review_outcomes=["pass"],
    )
    twin_snap = _kickoff_snapshot(
        build_topology_twin_flow,
        tmp_path=tmp_path / "twin",
        config=_base_config(),
        review_outcomes=["pass"],
    )

    assert class_snap == twin_snap
    assert class_snap["status"] == "completed"
    assert class_snap["review_status"] == "pass"
    assert class_snap["revisions"] == 0
    assert class_snap["last_stage"] == "finalize"
    assert class_snap["task_statuses"] == [(1, "done")]


def test_class_vs_twin_kickoff_revise_once_then_pass(tmp_path: Path):
    class_snap = _kickoff_snapshot(
        CrewAIHeadlessFlow,
        tmp_path=tmp_path / "class",
        config=_base_config(),
        review_outcomes=["revise", "pass"],
        continue_revise_cycle=True,
    )
    twin_snap = _kickoff_snapshot(
        build_topology_twin_flow,
        tmp_path=tmp_path / "twin",
        config=_base_config(),
        review_outcomes=["revise", "pass"],
        continue_revise_cycle=True,
    )

    assert class_snap == twin_snap
    assert class_snap["status"] == "completed"
    assert class_snap["review_status"] == "pass"
    assert class_snap["revisions"] == 1
    assert class_snap["last_stage"] == "finalize"
    assert class_snap["review_calls"] == 2


def test_class_vs_twin_kickoff_abort_before_plan(tmp_path: Path):
    cfg = _base_config(
        human_feedback={"enabled": True, "before_plan": True},
    )
    class_snap = _kickoff_snapshot(
        CrewAIHeadlessFlow,
        tmp_path=tmp_path / "class",
        config=cfg,
        review_outcomes=["pass"],
        abort_before_plan=True,
    )
    twin_snap = _kickoff_snapshot(
        build_topology_twin_flow,
        tmp_path=tmp_path / "twin",
        config=_base_config(
            human_feedback={"enabled": True, "before_plan": True},
        ),
        review_outcomes=["pass"],
        abort_before_plan=True,
    )

    assert class_snap == twin_snap
    assert class_snap["status"] == "aborted_by_human"
    assert class_snap["last_stage"] == "plan"
    assert class_snap["review_calls"] == 0
    assert any("Aborted by human before plan" in err for err in class_snap["errors"])


def test_class_vs_twin_kickoff_verify_fail_routes_to_revise(tmp_path: Path):
    cfg = _base_config(verify={"commands": ["pytest -q"], "mode": "gate"})
    class_snap = _kickoff_snapshot(
        CrewAIHeadlessFlow,
        tmp_path=tmp_path / "class",
        config=cfg,
        review_outcomes=["pass"],
        verification_runner=FakeVerifyRunner(exit_code=1),
    )
    twin_snap = _kickoff_snapshot(
        build_topology_twin_flow,
        tmp_path=tmp_path / "twin",
        config=_base_config(verify={"commands": ["pytest -q"], "mode": "gate"}),
        review_outcomes=["pass"],
        verification_runner=FakeVerifyRunner(exit_code=1),
    )

    assert class_snap == twin_snap
    assert class_snap["review_calls"] == 0
    assert class_snap["verification_passed"] == [False]
    assert class_snap["revisions"] == 1
    assert class_snap["issues_prefix"]
    assert "pytest -q" in class_snap["issues_prefix"][0]
