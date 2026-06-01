"""
Milestone 0 Spike: Minimal CrewAI Flow proving @start -> @listen -> @router topology
on a local Ollama LLM (orchestration tier, $0).

This is intentionally tiny — just enough to validate the control plane works.
Real skills, real workers, and real state come in later milestones.
"""

from __future__ import annotations

from typing import Literal

from crewai.flow.flow import Flow, listen, router, start
from pydantic import BaseModel


class SpikeState(BaseModel):
    request: str = "demo task"
    stage: str = "start"
    result: str = ""


class SpikeFlow(Flow[SpikeState]):
    """Trivial Flow demonstrating the exact decorator set the project will use."""

    @start()
    def begin(self) -> str:
        self.state.stage = "planning"
        print(f"[SPIKE] @start → planning (request={self.state.request})")
        # In a real stage we would call an Agent here with planning-and-task-breakdown skill.
        # For M0 we just return a tiny deterministic "spec".
        return "spec: add a hello function with test"

    @listen("begin")
    def do_work(self, spec: str) -> str:
        self.state.stage = "working"
        print(f"[SPIKE] @listen(begin) → do_work (spec={spec})")
        # Real milestone will inject skill Process text + call HeadlessCoder here.
        return "changes: hello.py + test_hello.py"

    @router("do_work")
    def review(self, work_output: str) -> Literal["pass", "revise"]:
        self.state.stage = "review"
        print(f"[SPIKE] @router(do_work) → review (work={work_output})")
        # Real milestone will call the worker in inspect mode with code-review-and-quality.
        # For spike we always "pass" so the graph completes.
        return "pass"

    @listen("pass")
    def finalize(self, review_decision: str) -> str:
        self.state.stage = "done"
        final = f"FINAL: {review_decision} | {self.state.result or 'demo complete'}"
        print(f"[SPIKE] @listen(pass) → finalize → {final}")
        self.state.result = final
        return final


def run_spike(request: str = "demo task") -> SpikeState:
    """Entry point for manual or test execution of the M0 spike."""
    flow = SpikeFlow()
    flow.state.request = request
    flow.kickoff()
    print(f"\n[SPIKE] Flow completed. Final state: {flow.state.model_dump()}")
    return flow.state


if __name__ == "__main__":
    run_spike("M0 spike request: prove the topology")
