"""
Pydantic state for the main CrewAI Headless Flow.

This state is persisted with @persist (SQLite by default in CrewAI Flows).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TaskItem(BaseModel):
    """A single task from the breakdown."""

    id: int
    description: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    status: Literal["pending", "in_progress", "done"] = "pending"


class FlowState(BaseModel):
    """
    Structured state for the entire reusable headless coding flow.
    """

    # Inputs
    request: str = ""
    target_repo: str = ""

    # Plan stage output
    spec: str | None = None
    tasks: list[TaskItem] = Field(default_factory=list)

    # Work + review state
    changed_files: list[str] = Field(default_factory=list)
    review_status: Literal["pending", "pass", "revise"] = "pending"
    issues: list[str] = Field(default_factory=list)

    # Bounded revise loop control
    revisions: int = 0
    max_revisions: int = 2

    # Final output
    final_artifact: str | None = None

    # Internal / diagnostics
    status: Literal["pending", "running", "completed", "aborted_by_human", "failed"] = (
        "pending"
    )
    aborted_stage: str | None = None
    last_stage: str | None = None
    errors: list[str] = Field(default_factory=list)

    @property
    def should_revise(self) -> bool:
        return self.review_status == "revise" and self.revisions < self.max_revisions

    def increment_revision(self) -> None:
        self.revisions += 1
        self.review_status = "pending"  # reset for next round
