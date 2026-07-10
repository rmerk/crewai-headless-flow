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

import time
from pathlib import Path
from typing import Callable, Optional

from ..skills.loader import SkillLoader, get_default_loader
from ..workers.base import CoderResult, HeadlessCoder, HeadlessCoderError, Mode

# Exit code recorded on a CoderResult synthesized from an infrastructure
# exception (WorkerTimeout / WorkerInvocationError). 124 mirrors the
# coreutils `timeout` convention.
INFRASTRUCTURE_FAILURE_EXIT_CODE = 124


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
        fallback_worker: HeadlessCoder | None = None,
        max_attempts: int = 1,
        backoff_seconds: float = 0.0,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.worker = worker
        self.skill_name = skill_name
        self.loader = loader or get_default_loader()
        self.fallback_worker = fallback_worker
        self.max_attempts = max(1, max_attempts)
        self.backoff_seconds = max(0.0, backoff_seconds)
        self._sleep = sleep_fn

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

        # Infrastructure exceptions (WorkerTimeout / WorkerInvocationError)
        # must never escape this seam: every direct call site and every crew
        # receives this tool, so converting them to a failed CoderResult here
        # routes hangs and launch failures into the Flow's ordinary
        # failure/revise machinery. Retries and the optional fallback worker
        # fire only on those exceptions — a returned non-zero exit is a
        # semantic failure and belongs to the revise loop, not a retry.
        last_error = "worker did not run"
        workers = [self.worker]
        if self.fallback_worker is not None:
            workers.append(self.fallback_worker)

        first_attempt = True
        for worker in workers:
            for _ in range(self.max_attempts):
                if not first_attempt and self.backoff_seconds:
                    self._sleep(self.backoff_seconds)
                first_attempt = False
                try:
                    return worker.run(
                        augmented_task,
                        cwd=cwd,
                        mode=mode,
                        schema=schema,
                        model=model,
                        timeout=timeout,
                    )
                except HeadlessCoderError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    print(f"[Worker] Infrastructure failure contained: {last_error}")

        return CoderResult(
            summary="",
            exit_code=INFRASTRUCTURE_FAILURE_EXIT_CODE,
            error=last_error,
        )

    def __call__(self, *args, **kwargs) -> CoderResult:
        """Allow using the tool instance like a function (nice for CrewAI tools later)."""
        return self.run(*args, **kwargs)
