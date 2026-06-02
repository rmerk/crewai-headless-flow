"""
Pluggable headless coding workers.

Public API:
    from crewai_headless_flow.workers import ClaudeAdapter, CodexAdapter, GrokAdapter, CoderResult, ReviewResult
"""

from .base import CoderResult, HeadlessCoder, ReviewResult
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .grok import GrokAdapter

__all__ = [
    "CoderResult",
    "ReviewResult",
    "HeadlessCoder",
    "ClaudeAdapter",
    "CodexAdapter",
    "GrokAdapter",
]
