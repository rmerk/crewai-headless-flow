"""Phase 0: FlowDefinition projection and declarative seam smokes (offline)."""

from __future__ import annotations

import os

import pytest
from crewai.flow.flow import Flow
from crewai.flow.flow_definition import FlowDefinition
from crewai.flow.runtime import FlowScriptExecutionDisabledError, build_flow_definition

from crewai_headless_flow.flow import CrewAIHeadlessFlow

pytestmark = pytest.mark.offline

# Canonical topology projected from the decorator Flow (source of truth until cutover flip).
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


def test_build_flow_definition_projects_canonical_topology():
    definition = build_flow_definition(CrewAIHeadlessFlow)

    assert definition.name == "CrewAIHeadlessFlow"
    assert set(definition.methods) == set(EXPECTED_METHODS)

    for name, expected in EXPECTED_METHODS.items():
        method = definition.methods[name]
        assert method.start == expected["start"], name
        assert method.listen == expected["listen"], name
        assert method.router is expected["router"], name
        assert method.emit == expected["emit"], name
        assert method.do.call == "code", name
        assert method.do.ref == (
            f"crewai_headless_flow.flow:CrewAIHeadlessFlow.{name}"
        ), name


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
