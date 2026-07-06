"""
Pluggable headless coding workers.

Public API:
    from crewai_headless_flow.workers import ClaudeAdapter, CodexAdapter, CursorAdapter, GeminiAdapter, GrokAdapter, CoderResult, ReviewResult
"""

from .base import CoderResult, HeadlessCoder, ReviewResult
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .cursor import CursorAdapter
from .gemini import GeminiAdapter
from .grok import GrokAdapter

__all__ = [
    "CoderResult",
    "ReviewResult",
    "HeadlessCoder",
    "ClaudeAdapter",
    "CodexAdapter",
    "CursorAdapter",
    "GeminiAdapter",
    "GrokAdapter",
]
