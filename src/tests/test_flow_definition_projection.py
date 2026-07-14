"""Phase 0/3: FlowDefinition declarative seam smokes + canonical topology (offline)."""

from __future__ import annotations

import os
from typing import Any

import pytest
from crewai.flow.flow import Flow
from crewai.flow.flow_definition import FlowDefinition
from crewai.flow.runtime import FlowScriptExecutionDisabledError

from crewai_headless_flow.flow_topology import load_flow_definition

pytestmark = pytest.mark.offline

# Canonical topology from config/flow.yaml (SoT after Phase 3 entrypoint flip).
EXPECTED_METHODS = {
    "plan": {
        "start": True,
        "listen": None,
        "router": False,
        "emit": None,
    },
    "do_work": {
        "start": None,
        "listen": {"or": ["plan", "process_revision"]},
        "router": False,
        "emit": None,
    },
    "review": {
        "start": None,
        "listen": "do_work",
        "router": True,
        "emit": ["pass", "revise", "aborted"],
    },
    "process_revision": {
        "start": None,
        "listen": "revise",
        "router": False,
        "emit": None,
    },
    "finalize": {
        "start": None,
        "listen": "pass",
        "router": False,
        "emit": None,
    },
    "handle_aborted": {
        "start": None,
        "listen": "aborted",
        "router": False,
        "emit": None,
    },
    "handle_failed": {
        "start": None,
        "listen": "failed",
        "router": False,
        "emit": None,
    },
}

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


def _double_bound_value(self: Any, value: int) -> int:
    """Code-action helper: unbound callables are bound to the Flow as methods."""
    return value * 2


def test_load_flow_definition_is_canonical_topology_source_of_truth():
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


def test_flow_definition_accepts_call_code_declaration():
    definition = FlowDefinition.from_declaration(
        contents={
            "schema": "crewai.flow/v1",
            "name": "CodeSeamSmoke",
            "state": {"type": "dict", "default": {}},
            "methods": {
                "seed": {
                    "start": True,
                    "do": {
                        "call": "code",
                        "ref": "builtins:len",
                        "with": {"obj": []},
                    },
                }
            },
        }
    )

    action = definition.methods["seed"].do
    assert action.call == "code"
    assert action.ref == "builtins:len"


def test_flow_definition_parses_script_tool_and_human_feedback():
    definition = FlowDefinition.from_declaration(
        contents={
            "schema": "crewai.flow/v1",
            "name": "ActionShapeSmoke",
            "state": {"type": "dict", "default": {}},
            "methods": {
                "script_step": {
                    "start": True,
                    "do": {
                        "call": "script",
                        "code": "return 1",
                    },
                },
                "tool_step": {
                    "listen": "script_step",
                    "do": {
                        "call": "tool",
                        "ref": "builtins:len",
                    },
                },
                "hf_step": {
                    "listen": "tool_step",
                    "human_feedback": {
                        "message": "choose",
                        "emit": ["ok", "retry"],
                    },
                    "do": {"call": "expression", "expr": "'done'"},
                },
            },
        }
    )

    assert definition.methods["script_step"].do.call == "script"
    assert definition.methods["tool_step"].do.call == "tool"
    assert definition.methods["tool_step"].do.ref == "builtins:len"
    assert definition.methods["hf_step"].human_feedback is not None
    assert definition.methods["hf_step"].human_feedback.emit == ["ok", "retry"]


def test_human_feedback_emit_canonicalizes_router_and_clears_method_emit():
    definition = FlowDefinition.from_declaration(
        contents={
            "schema": "crewai.flow/v1",
            "name": "HumanFeedbackCanonicalizeSmoke",
            "state": {"type": "dict", "default": {}},
            "methods": {
                "seed": {
                    "start": True,
                    "emit": ["x", "y"],
                    "human_feedback": {
                        "message": "pick",
                        "emit": ["a", "b"],
                    },
                    "do": {"call": "expression", "expr": "'seed'"},
                }
            },
        }
    )

    method = definition.methods["seed"]
    assert method.router is True
    assert method.emit is None
    assert method.human_feedback is not None
    assert method.human_feedback.emit == ["a", "b"]


def test_flow_from_declaration_rejects_script_without_opt_in(monkeypatch):
    monkeypatch.delenv("CREWAI_ALLOW_FLOW_SCRIPT_EXECUTION", raising=False)

    with pytest.raises(FlowScriptExecutionDisabledError):
        Flow.from_declaration(
            contents={
                "schema": "crewai.flow/v1",
                "name": "ScriptSeamSmoke",
                "state": {"type": "dict", "default": {"n": 1}},
                "methods": {
                    "seed": {
                        "start": True,
                        "do": {
                            "call": "script",
                            "code": "state['n'] = state['n'] + 1\nreturn state['n']",
                        },
                    }
                },
            }
        )

    assert os.environ.get("CREWAI_ALLOW_FLOW_SCRIPT_EXECUTION") is None


def test_flow_from_declaration_script_runs_with_opt_in(monkeypatch):
    monkeypatch.setenv("CREWAI_ALLOW_FLOW_SCRIPT_EXECUTION", "1")

    flow = Flow.from_declaration(
        contents={
            "schema": "crewai.flow/v1",
            "name": "ScriptOptInSmoke",
            "state": {"type": "dict", "default": {"n": 1}},
            "methods": {
                "seed": {
                    "start": True,
                    "do": {
                        "call": "script",
                        "code": "state['n'] = state['n'] + 1\nreturn state['n']",
                    },
                }
            },
        },
        suppress_flow_events=True,
    )

    assert flow.kickoff() == 2
    assert flow.state["n"] == 2


def test_flow_from_declaration_call_code_uses_bound_self_and_outputs():
    flow = Flow.from_declaration(
        contents={
            "schema": "crewai.flow/v1",
            "name": "CodeKickoffSmoke",
            "state": {"type": "dict", "default": {}},
            "methods": {
                "seed": {
                    "start": True,
                    "do": {"call": "expression", "expr": "3"},
                },
                "double": {
                    "listen": "seed",
                    "do": {
                        "call": "code",
                        "ref": (
                            "tests.test_flow_definition_projection:_double_bound_value"
                        ),
                        "with": {"value": "${outputs.seed}"},
                    },
                },
            },
        },
        suppress_flow_events=True,
    )

    assert flow.kickoff() == 6


def test_flow_from_declaration_listen_or_parses_and_runs():
    definition = FlowDefinition.from_declaration(
        contents={
            "schema": "crewai.flow/v1",
            "name": "OrListenParseSmoke",
            "state": {"type": "dict", "default": {}},
            "methods": {
                "a": {"start": True, "do": {"call": "expression", "expr": "'a'"}},
                "c": {"do": {"call": "expression", "expr": "'c'"}},
                "join": {
                    "listen": {"or": ["a", "c"]},
                    "do": {"call": "expression", "expr": "outputs.a"},
                },
            },
        }
    )
    assert definition.methods["join"].listen == {"or": ["a", "c"]}

    flow = Flow.from_declaration(
        contents={
            "schema": "crewai.flow/v1",
            "name": "OrListenRunSmoke",
            "state": {"type": "dict", "default": {}},
            "methods": {
                "a": {"start": True, "do": {"call": "expression", "expr": "'from-a'"}},
                "c": {"do": {"call": "expression", "expr": "'from-c'"}},
                "join": {
                    "listen": {"or": ["a", "c"]},
                    "do": {"call": "expression", "expr": "outputs.a"},
                },
            },
        },
        suppress_flow_events=True,
    )
    assert flow.kickoff() == "from-a"


def test_flow_from_declaration_binds_pydantic_flow_state():
    flow = Flow.from_declaration(
        contents={
            "schema": "crewai.flow/v1",
            "name": "PydanticStateSmoke",
            "state": {
                "type": "pydantic",
                "ref": "crewai_headless_flow.state:FlowState",
                "default": {"request": "hello-req"},
            },
            "methods": {
                "seed": {
                    "start": True,
                    "do": {"call": "expression", "expr": "state.request"},
                }
            },
        },
        suppress_flow_events=True,
    )

    assert flow.kickoff() == "hello-req"
    assert flow.state.request == "hello-req"
