"""Terminal aborted/failed handlers (Phase 1 extraction)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _stop_at_terminal_status(flow: Any, _decision: str) -> str:
    # Method name on the Flow must not match the router event it listens to
    # (CrewAI 1.15+); aborted and failed share the same stop behavior.
    logger.info(f"[Flow] Flow stopped at terminal status: {flow.state.status}")
    return flow._terminal_result()


def execute_handle_aborted(flow: Any, _decision: str) -> str:
    return _stop_at_terminal_status(flow, _decision)


def execute_handle_failed(flow: Any, _decision: str) -> str:
    return _stop_at_terminal_status(flow, _decision)
