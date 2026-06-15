"""Programmatic entrypoints for crewai-headless-flow."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

from .config import DEFAULT_CONFIG_DIR, FlowConfig, load_config
from .reporting import render_execution_report
from .runtime_overrides import load_runtime_config
from .state import FlowState

if TYPE_CHECKING:
    from .flow import CrewAIHeadlessFlow, resume_headless_flow, run_headless_flow

try:
    __version__ = version("crewai-headless-flow")
except PackageNotFoundError:  # pragma: no cover - fallback for unusual local imports
    __version__ = "0.0.0"

__all__ = [
    "__version__",
    "CrewAIHeadlessFlow",
    "DEFAULT_CONFIG_DIR",
    "FlowConfig",
    "FlowState",
    "load_config",
    "load_runtime_config",
    "render_execution_report",
    "resume_headless_flow",
    "run_headless_flow",
]


def __getattr__(name: str) -> Any:
    if name in {"CrewAIHeadlessFlow", "resume_headless_flow", "run_headless_flow"}:
        from .flow import CrewAIHeadlessFlow, resume_headless_flow, run_headless_flow

        exports = {
            "CrewAIHeadlessFlow": CrewAIHeadlessFlow,
            "resume_headless_flow": resume_headless_flow,
            "run_headless_flow": run_headless_flow,
        }
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
