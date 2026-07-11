"""
GeminiAdapter - maps the HeadlessCoder protocol to Gemini CLI invocations.

Key normalizations:
- edit mode -> real target repository with --approval-mode yolo.
- inspect mode -> disposable filesystem copy plus --approval-mode plan.
- structured output -> prompt-injected JSON schema plus one repair retry.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .base import (
    CoderResult,
    Mode,
    WorkerInvocationError,
    WorkerTimeout,
    ignore_uncopyable,
    sanitize_cwd,
)
from .structured_output import build_repair_prompt, extract_validated_json


class GeminiAdapter:
    """Concrete adapter for Gemini CLI (`gemini`)."""

    def __init__(self, binary: str = "gemini") -> None:
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

        if mode == "inspect":
            workdir = self._create_disposable_copy(original_workdir)
            approval_mode = "plan"
        else:
            workdir = original_workdir
            approval_mode = "yolo"

        try:
            final_task = task
            if schema:
                schema_str = json.dumps(schema, indent=2)
                final_task = (
                    f"{task}\n\n"
                    "Respond with only a single valid JSON object that matches "
                    "this exact schema (no extra text, no markdown fences):\n"
                    f"{schema_str}\n"
                    "If the schema is not satisfied, the result will be rejected."
                )

            cmd = self._build_command(
                task=final_task,
                approval_mode=approval_mode,
                model=model,
            )

            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=workdir,
                )
            except subprocess.TimeoutExpired as exc:
                raise WorkerTimeout(f"Gemini timed out after {timeout}s") from exc
            except Exception as exc:
                raise WorkerInvocationError(f"Failed to invoke Gemini: {exc}") from exc

            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            exit_code = proc.returncode

            parsed_summary = extract_validated_json(stdout, schema)
            if schema and not parsed_summary and exit_code == 0:
                repair_cmd = self._build_command(
                    task=build_repair_prompt(task, schema, stdout),
                    approval_mode=approval_mode,
                    model=model,
                )
                repair_proc = subprocess.run(
                    repair_cmd,
                    capture_output=True,
                    text=True,
                    timeout=min(120, timeout),
                    cwd=workdir,
                )
                exit_code = repair_proc.returncode
                stdout = repair_proc.stdout or stdout
                stderr = repair_proc.stderr or stderr
                parsed_summary = extract_validated_json(stdout, schema)

            return CoderResult(
                summary=parsed_summary or self._extract_summary(stdout, stderr),
                changed_files=[],
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
        *,
        task: str,
        approval_mode: str,
        model: Optional[str],
    ) -> list[str]:
        cmd = [
            self.binary,
            "--prompt",
            task,
            "--output-format",
            "json",
            "--approval-mode",
            approval_mode,
            "--skip-trust",
        ]
        if model:
            cmd += ["--model", model]
        return cmd

    def _create_disposable_copy(self, src: Path) -> Path:
        tmp_root = Path(tempfile.mkdtemp(prefix="gemini-inspect-"))
        dst = tmp_root / src.name
        try:
            shutil.copytree(
                src,
                dst,
                symlinks=False,
                ignore=ignore_uncopyable,
                ignore_dangling_symlinks=True,
            )
        except Exception:
            shutil.rmtree(tmp_root, ignore_errors=True)
            raise
        return dst

    def _cleanup_disposable(self, path: Path) -> None:
        if path.exists() and "gemini-inspect-" in str(path.parent):
            shutil.rmtree(path, ignore_errors=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass

    def _extract_summary(self, stdout: str, stderr: str) -> str:
        raw = stdout.strip()
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = None

            if isinstance(data, dict):
                response = data.get("response")
                if isinstance(response, str):
                    return response
                if response is not None:
                    return json.dumps(response, indent=2)
                error = data.get("error")
                if isinstance(error, dict):
                    return json.dumps(error, indent=2)
                for key in ("summary", "message", "content"):
                    value = data.get(key)
                    if isinstance(value, str):
                        return value
                    if value is not None:
                        return json.dumps(value, indent=2)

        lines = [
            line.strip()
            for line in (stdout + "\n" + stderr).splitlines()
            if line.strip()
        ]
        return lines[-1][:600] if lines else "Gemini completed"
