"""
Pluggable headless coding workers.

Public API:
    from crewai_headless_flow.workers import CodexAdapter, GrokAdapter, CoderResult, ReviewResult
"""

from .base import CoderResult, HeadlessCoder, ReviewResult
from .codex import CodexAdapter
from .grok import GrokAdapter

__all__ = [
    "CoderResult",
    "ReviewResult",
    "HeadlessCoder",
    "CodexAdapter",
    "GrokAdapter",
]
