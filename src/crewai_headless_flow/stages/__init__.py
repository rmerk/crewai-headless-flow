"""Importable stage callables extracted from CrewAIHeadlessFlow (Phase 1).

Class methods on CrewAIHeadlessFlow remain thin wrappers that call these
functions so decorator topology and offline tests stay unchanged.
"""

from __future__ import annotations

from .finalize import execute_finalize
from .plan import execute_plan
from .do_work import execute_do_work
from .review import execute_review
from .revision import execute_process_revision
from .terminal import execute_handle_aborted, execute_handle_failed

__all__ = [
    "execute_do_work",
    "execute_finalize",
    "execute_handle_aborted",
    "execute_handle_failed",
    "execute_plan",
    "execute_process_revision",
    "execute_review",
]
