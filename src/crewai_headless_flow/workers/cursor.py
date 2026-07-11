"""
CursorAdapter - maps the HeadlessCoder protocol to Cursor Agent CLI invocations.

Key normalizations:
- edit mode -> real target repository with --force and --trust.
- inspect mode -> disposable filesystem copy plus --plan and --trust.
- structured output -> prompt-injected JSON schema plus one repair retry.
- auth -> inherited CURSOR_API_KEY from the process environment (never read dotfiles).
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


class CursorAdapter:
    """Concrete adapter for Cursor Agent CLI (`cursor agent`)."""

    def __init__(self, binary: str = "cursor") -> None:
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
            headless_mode = "plan"
        else:
            workdir = original_workdir
            headless_mode = "force"

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
                workdir=workdir,
                headless_mode=headless_mode,
                model=model,
            )

            try:
                proc = self._run_subprocess(cmd, cwd=workdir, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                raise WorkerTimeout(f"Cursor timed out after {timeout}s") from exc
            except Exception as exc:
                raise WorkerInvocationError(f"Failed to invoke Cursor: {exc}") from exc

            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            exit_code = proc.returncode

            parsed_summary = extract_validated_json(stdout, schema)
            if schema and not parsed_summary and exit_code == 0:
                repair_cmd = self._build_command(
                    task=build_repair_prompt(task, schema, stdout),
                    workdir=workdir,
                    headless_mode=headless_mode,
                    model=model,
                )
                repair_proc = self._run_subprocess(
                    repair_cmd, cwd=workdir, timeout=min(120, timeout)
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

    def _run_subprocess(
        self, cmd: list[str], *, cwd: Path, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        """
        Invoke `cmd` with real temp files backing stdout/stderr instead of pipes.

        The Cursor Agent CLI hangs indefinitely when given pipe-backed stdio
        while the parent process's own stderr fd has already been redirected
        to a plain file rather than a TTY -- exactly what pytest's default
        fd-level output capture (and many CI log wrappers) do. File-backed
        capture avoids that trigger entirely while still giving us the
        subprocess's output afterward.
        """
        with (
            tempfile.TemporaryFile() as out_f,
            tempfile.TemporaryFile() as err_f,
        ):
            proc = subprocess.run(
                cmd,
                stdout=out_f,
                stderr=err_f,
                timeout=timeout,
                cwd=cwd,
            )
            out_f.seek(0)
            err_f.seek(0)
            stdout = out_f.read().decode("utf-8", errors="replace")
            stderr = err_f.read().decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)

    def _build_command(
        self,
        *,
        task: str,
        workdir: Path,
        headless_mode: str,
        model: Optional[str],
    ) -> list[str]:
        mode_flag = "--plan" if headless_mode == "plan" else "--force"
        cmd = [
            self.binary,
            "agent",
            "--print",
            "--output-format",
            "json",
            mode_flag,
            "--trust",
            "--workspace",
            str(workdir),
        ]
        if model:
            cmd += ["--model", model]
        cmd.append(task)
        return cmd

    def _create_disposable_copy(self, src: Path) -> Path:
        tmp_root = Path(tempfile.mkdtemp(prefix="cursor-inspect-"))
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
        if path.exists() and "cursor-inspect-" in str(path.parent):
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
                for key in ("summary", "content", "message", "text"):
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
        return lines[-1][:600] if lines else "Cursor completed"
