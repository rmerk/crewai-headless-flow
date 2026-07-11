"""
GrokAdapter — maps the HeadlessCoder protocol to the `grok` CLI (xAI).

Key normalizations implemented here (as required by the design):
- edit mode   → passes --always-approve (full non-interactive mutation)
- inspect mode → NEVER passes --always-approve.
                 Instead, we run against a disposable filesystem copy so an
                 auto-approving worker cannot
                 mutate the caller's original tree.
- Structured output: We cannot use --output-schema. We request exact JSON
  in the prompt and validate with Pydantic. One repair retry on failure.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, TypeVar

from pydantic import BaseModel

from .base import (
    CoderResult,
    Mode,
    WorkerInvocationError,
    WorkerTimeout,
    ignore_uncopyable,
    sanitize_cwd,
)
from .structured_output import build_repair_prompt, extract_validated_json

T = TypeVar("T", bound=BaseModel)


class GrokAdapter:
    """Concrete adapter for the Grok Build CLI (`grok`)."""

    def __init__(self, binary: str = "grok") -> None:
        self.binary = binary

    def run(
        self,
        task: str,
        cwd: str | Path,
        mode: Mode = "edit",
        schema: Optional[dict] = None,
        model: Optional[str] = None,
        timeout: int = 300,
    ) -> CoderResult:
        original_workdir = sanitize_cwd(cwd)
        original_workdir.mkdir(parents=True, exist_ok=True)

        # === THE KEY NORMALIZATION FOR GROK (no native read-only sandbox in all contexts) ===
        if mode == "inspect":
            # Run against a throwaway copy so we cannot mutate the real tree.
            workdir = self._create_disposable_copy(original_workdir)
            auto_approve = False
        else:
            workdir = original_workdir
            auto_approve = True

        try:
            final_task = task
            if schema:
                # Inject strict JSON instruction for Grok (it has no --output-schema)
                schema_str = json.dumps(schema, indent=2)
                final_task = (
                    f"{task}\n\n"
                    f"Respond with **only** a single valid JSON object that matches "
                    f"this exact schema (no extra text, no markdown fences):\n"
                    f"{schema_str}\n"
                    f"If the schema is not satisfied, the result will be rejected."
                )

            cmd = self._build_command(final_task, workdir, auto_approve, model)

            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=workdir,
                )
            except subprocess.TimeoutExpired as e:
                raise WorkerTimeout(f"Grok timed out after {timeout}s") from e
            except Exception as e:
                raise WorkerInvocationError(f"Failed to invoke Grok: {e}") from e

            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            exit_code = proc.returncode

            # Attempt structured parse + one repair retry for Grok
            parsed_summary = extract_validated_json(stdout, schema)

            if schema and not parsed_summary and exit_code == 0:
                # One repair retry
                repair_task = build_repair_prompt(task, schema, stdout)
                repair_cmd = self._build_command(
                    repair_task, workdir, auto_approve, model
                )
                try:
                    repair_proc = subprocess.run(
                        repair_cmd,
                        capture_output=True,
                        text=True,
                        timeout=min(120, timeout),
                        cwd=workdir,
                    )
                    stdout = repair_proc.stdout or stdout
                    stderr = repair_proc.stderr or stderr
                    exit_code = repair_proc.returncode
                    parsed_summary = extract_validated_json(stdout, schema)
                except Exception:
                    pass

            summary = parsed_summary or self._extract_text_summary(stdout, stderr)

            return CoderResult(
                summary=summary,
                changed_files=[],  # Caller can inspect git diff after the fact
                tests_passed=False,
                raw_output=stdout,
                exit_code=exit_code,
                error=stderr if exit_code != 0 else None,
            )

        finally:
            if mode == "inspect":
                self._cleanup_disposable(workdir)

    def _build_command(
        self,
        task: str,
        cwd: Path,
        auto_approve: bool,
        model: Optional[str],
    ) -> list[str]:
        cmd = [
            self.binary,
            "-p",
            task,  # --single
            "--cwd",
            str(cwd),
            "--output-format",
            "json",
            "--no-alt-screen",
            "--no-auto-update",
        ]
        if auto_approve:
            cmd.append("--always-approve")
        if model:
            cmd += ["-m", model]
        return cmd

    def _create_disposable_copy(self, src: Path) -> Path:
        """
        Create a clean, throwaway copy of the repo for inspect mode.
        Using a plain copytree is the safest portable approach (no git index pollution).
        A real git worktree would also work; copytree is simpler and always clean.
        """
        tmp_root = Path(tempfile.mkdtemp(prefix="grok-inspect-"))
        dst = tmp_root / src.name
        shutil.copytree(
            src,
            dst,
            symlinks=False,
            ignore=ignore_uncopyable,
            ignore_dangling_symlinks=True,
        )
        return dst

    def _cleanup_disposable(self, path: Path) -> None:
        if path.exists() and "grok-inspect-" in str(path.parent):
            shutil.rmtree(path, ignore_errors=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass

    def _extract_text_summary(self, stdout: str, stderr: str) -> str:
        lines = [
            line.strip()
            for line in (stdout + "\n" + stderr).splitlines()
            if line.strip()
        ]
        for line in reversed(lines[-15:]):
            if not line.startswith("{") and not line.startswith("["):
                return line[:600]
        return (stdout or stderr or "Grok completed").strip()[:600]
