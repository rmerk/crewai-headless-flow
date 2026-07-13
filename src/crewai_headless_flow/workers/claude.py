"""
ClaudeAdapter - maps the HeadlessCoder protocol to Claude Code CLI invocations.

Key normalizations:
- edit mode -> real target repository with bypassPermissions.
- inspect mode -> disposable filesystem copy plus dontAsk permissions.
- structured output -> native Claude Code --json-schema in print mode.
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
)
from .structured_output import build_repair_prompt, extract_validated_json


class ClaudeAdapter:
    """Concrete adapter for Claude Code CLI (`claude`)."""

    def __init__(self, binary: str = "claude", effort: Optional[str] = None) -> None:
        self.binary = binary
        self.effort = effort

    def run(
        self,
        task: str,
        cwd: str | Path,
        mode: Mode = "edit",
        schema: Optional[dict] = None,
        model: Optional[str] = None,
        timeout: int = 300,
    ) -> CoderResult:
        original_workdir = self._normalize_cwd(cwd)
        original_workdir.mkdir(parents=True, exist_ok=True)

        if mode == "inspect":
            workdir = self._create_disposable_copy(original_workdir)
            permission_mode = "dontAsk"
        else:
            workdir = original_workdir
            permission_mode = "bypassPermissions"

        try:
            cmd = self._build_command(
                task=task,
                permission_mode=permission_mode,
                schema=schema,
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
            except subprocess.TimeoutExpired as e:
                raise WorkerTimeout(f"Claude timed out after {timeout}s") from e
            except Exception as e:
                raise WorkerInvocationError(f"Failed to invoke Claude: {e}") from e

            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            exit_code = proc.returncode

            parsed_summary = extract_validated_json(stdout, schema)
            if schema and not parsed_summary and exit_code == 0:
                repair_cmd = self._build_command(
                    task=build_repair_prompt(task, schema, stdout),
                    permission_mode=permission_mode,
                    schema=schema,
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
        permission_mode: str,
        schema: Optional[dict],
        model: Optional[str],
    ) -> list[str]:
        cmd = [
            self.binary,
            "-p",
            task,
            "--output-format",
            "json",
            "--permission-mode",
            permission_mode,
        ]
        if schema:
            cmd += ["--json-schema", json.dumps(schema)]
        if model:
            cmd += ["--model", model]
        if self.effort:
            cmd += ["--effort", self.effort]
        return cmd

    def _create_disposable_copy(self, src: Path) -> Path:
        tmp_root = Path(tempfile.mkdtemp(prefix="claude-inspect-"))
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

    def _normalize_cwd(self, cwd: str | Path) -> Path:
        path = Path(cwd).expanduser()
        if path.is_absolute():
            return path
        return path.resolve(strict=False)

    def _cleanup_disposable(self, path: Path) -> None:
        if path.exists() and "claude-inspect-" in str(path.parent):
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
                result = data.get("result")
                if isinstance(result, str):
                    return result
                if result is not None:
                    return json.dumps(result, indent=2)
                for key in ("summary", "content", "message"):
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
        return lines[-1][:600] if lines else "Claude completed"
