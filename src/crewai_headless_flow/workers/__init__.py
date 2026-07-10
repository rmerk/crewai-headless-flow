"""
Pluggable headless coding workers.

Public API:
    from crewai_headless_flow.workers import ClaudeAdapter, CodexAdapter, CursorAdapter, GeminiAdapter, GrokAdapter, CoderResult, ReviewResult

``WORKER_SPECS`` is the single registration table (autonomy Gap 10): the
Flow's ``WORKER_ADAPTERS`` and all of doctor's worker dicts are derived from
it, so adding a worker is an adapter file plus one entry here.
"""

from dataclasses import dataclass

from .base import CoderResult, HeadlessCoder, ReviewResult
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .cursor import CursorAdapter
from .gemini import GeminiAdapter
from .grok import GrokAdapter


@dataclass(frozen=True)
class WorkerSpec:
    """Everything the platform needs to know about one worker.

    ``binary`` is the default executable name; operators can point a worker
    at a different binary via the top-level ``workers:`` config block.
    ``help_command``/``required_flags`` drive doctor's CLI probes — when the
    binary is overridden, doctor substitutes ``help_command[0]``.
    """

    adapter_cls: type[HeadlessCoder]
    binary: str
    help_command: tuple[str, ...]
    required_flags: tuple[str, ...]


WORKER_SPECS: dict[str, WorkerSpec] = {
    "codex": WorkerSpec(
        adapter_cls=CodexAdapter,
        binary="codex",
        help_command=("codex", "exec", "--help"),
        required_flags=("--sandbox", "--output-schema"),
    ),
    "grok": WorkerSpec(
        adapter_cls=GrokAdapter,
        binary="grok",
        help_command=("grok", "--help"),
        required_flags=("--always-approve", "--output-format"),
    ),
    "claude": WorkerSpec(
        adapter_cls=ClaudeAdapter,
        binary="claude",
        help_command=("claude", "--help"),
        required_flags=("--permission-mode", "--json-schema"),
    ),
    "gemini": WorkerSpec(
        adapter_cls=GeminiAdapter,
        binary="gemini",
        help_command=("gemini", "--help"),
        required_flags=("--prompt", "--approval-mode", "--output-format"),
    ),
    "cursor": WorkerSpec(
        adapter_cls=CursorAdapter,
        binary="cursor",
        help_command=("cursor", "agent", "--help"),
        required_flags=(
            "--print",
            "--output-format",
            "--plan",
            "--force",
            "--trust",
            "--workspace",
            "--model",
        ),
    ),
}

__all__ = [
    "CoderResult",
    "ReviewResult",
    "HeadlessCoder",
    "ClaudeAdapter",
    "CodexAdapter",
    "CursorAdapter",
    "GeminiAdapter",
    "GrokAdapter",
    "WorkerSpec",
    "WORKER_SPECS",
]
