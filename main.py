#!/usr/bin/env python
"""
Milestone 0 entry point.
Runs the trivial @start → @listen → @router Flow spike on the orchestration LLM.

Usage:
    uv run python main.py
    # or with Ollama running:
    # OLLAMA_MODEL=llama3.2 uv run python main.py
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from crewai_headless_flow.flow_spike import run_spike


def main() -> None:
    load_dotenv()

    model = os.getenv("OLLAMA_MODEL", "ollama/llama3.2")
    print("=== crewai-headless-flow M0 Spike ===")
    print(f"Orchestration model hint: {model}")
    print("Starting trivial Flow (no real LLM call in this minimal spike version)...\n")

    # M0 spike is deliberately deterministic (no LLM) to prove the decorator topology
    # works before we wire real Agents/LLMs in later milestones.
    state = run_spike("Prove CrewAI Flow + decorators on local orchestration tier")

    print("\n=== M0 Spike Result ===")
    print(f"Stage reached: {state.stage}")
    print(f"Result: {state.result}")
    print("SUCCESS: @start → @listen → @router topology executed cleanly.")


if __name__ == "__main__":
    main()
