"""Terminal aborted/failed handlers (Phase 1 extraction)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def execute_handle_aborted(flow: Any, _decision: str) -> str:
    # Method name must not match the router event it listens to (CrewAI 1.15+).
    logger.info(f"[Flow] Flow stopped at terminal status: {flow.state.status}")
    return flow._terminal_result()


def execute_handle_failed(flow: Any, _decision: str) -> str:
    # Method name must not match the router event it listens to (CrewAI 1.15+).
    logger.info(f"[Flow] Flow stopped at terminal status: {flow.state.status}")
    return flow._terminal_result()
