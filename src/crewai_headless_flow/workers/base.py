"""
HeadlessCoder interface + result types.

This is the single abstraction that lets the Flow (and tests) treat
Codex, Grok, Claude, and Gemini identically while the adapters handle all CLI differences.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional, Protocol


Mode = Literal["inspect", "edit"]


@dataclass(frozen=True)
class CoderResult:
    """Normalized result from any headless coding worker."""

    summary: str
    changed_files: list[str] = field(default_factory=list)
    tests_passed: bool = False
    raw_output: str = ""
    exit_code: int = 0
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.error


@dataclass(frozen=True)
class ReviewResult:
    """Structured result from an inspect/review run."""

    status: Literal["pass", "revise"]
    issues: list[str] = field(default_factory=list)
    raw_output: str = ""
    exit_code: int = 0
    error: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.status == "pass" and not self.error


class HeadlessCoder(Protocol):
    """
    Pluggable headless coding worker.

    The Flow (and any CrewAI Tool wrapper) calls this. The concrete
    adapter translates the call into the real CLI argv + sandboxing
    + structured output strategy for that specific tool.
    """

    def run(
        self,
        task: str,
        cwd: str | Path,
        mode: Mode = "edit",
        schema: Optional[dict] = None,
        model: Optional[str] = None,
        timeout: int = 300,
    ) -> CoderResult:
        """
        Execute the task in the given working directory.

        Args:
            task: The full prompt/instructions (may already contain
                  injected skill Process text + schema instructions).
            cwd: Target repository root (will be made absolute).
            mode: "edit" = may mutate files (full approvals/sandbox-write).
                  "inspect" = read-only guarantee (sandbox read-only or
                  disposable copy for workers without native sandbox).
            schema: Optional JSON Schema dict. When provided, the adapter
                    should try to enforce this shape on the final output.
            model: Optional model override for the worker.
            timeout: Hard timeout in seconds.

        Returns:
            CoderResult with normalized fields. For review-style calls,
            callers will usually parse the raw_output into a ReviewResult.
        """
        ...


class HeadlessCoderError(Exception):
    """Base error for worker failures (timeouts, non-zero exits, etc.)."""


class WorkerTimeout(HeadlessCoderError):
    """The worker exceeded the given timeout."""


class WorkerInvocationError(HeadlessCoderError):
    """The worker process failed in a way we can surface usefully."""


def sanitize_cwd(cwd: str | Path) -> Path:
    """Return an absolute, resolved Path for cwd."""
    return Path(cwd).resolve(strict=False)
