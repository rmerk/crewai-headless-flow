"""
HeadlessCoderTool + skill injection wiring.

This is the bridge between:
- SkillLoader (operating procedures from agent-skills)
- HeadlessCoder adapters (the actual "hands" that edit/run/test code)

The key responsibility for Milestone 3:
    When a skill is mapped to a stage, its core Process/guidance text
    MUST be injected into the task/prompt sent to the worker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..skills.loader import SkillLoader, get_default_loader
from ..workers.base import CoderResult, HeadlessCoder, Mode


def build_task_with_skill(
    skill_name: str,
    user_prompt: str,
    loader: Optional[SkillLoader] = None,
) -> str:
    """
    Prepend the chosen skill's operating procedure to the user's request.

    This is the critical "skills as operating procedures" pillar.
    The resulting string becomes the `task` argument passed to any HeadlessCoder.
    """
    loader = loader or get_default_loader()
    guidance = loader.get_core_guidance(skill_name)

    return f"""You must follow this exact operating procedure:

{guidance}

---

Current user request / context:
{user_prompt}

Remember: Execute according to the procedure above. Be explicit about which steps you are following.
""".strip()


class HeadlessCoderTool:
    """
    Thin wrapper that combines a HeadlessCoder worker with optional skill guidance.

    Usage (will become the CrewAI @tool in later milestones):
        tool = HeadlessCoderTool(worker=CodexAdapter(), skill_name="incremental-implementation")
        result = tool.run("Add feature X", cwd="/path/to/repo", mode="edit")
    """

    def __init__(
        self,
        worker: HeadlessCoder,
        skill_name: Optional[str] = None,
        loader: Optional[SkillLoader] = None,
    ) -> None:
        self.worker = worker
        self.skill_name = skill_name
        self.loader = loader or get_default_loader()

    def run(
        self,
        task: str,
        cwd: str | Path,
        mode: Mode = "edit",
        schema: Optional[dict] = None,
        model: Optional[str] = None,
        timeout: int = 300,
    ) -> CoderResult:
        if self.skill_name:
            augmented_task = build_task_with_skill(
                self.skill_name, task, loader=self.loader
            )
        else:
            augmented_task = task

        return self.worker.run(
            augmented_task,
            cwd=cwd,
            mode=mode,
            schema=schema,
            model=model,
            timeout=timeout,
        )

    def __call__(self, *args, **kwargs) -> CoderResult:
        """Allow using the tool instance like a function (nice for CrewAI tools later)."""
        return self.run(*args, **kwargs)
