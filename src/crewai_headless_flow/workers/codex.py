"""
CodexAdapter — maps the HeadlessCoder protocol to real `codex exec` invocations.

Observed on codex-cli 0.132.0:
- -C / --cd <dir>
- -s / --sandbox read-only | workspace-write
- --json
- --output-schema <file>
- --output-last-message <file>
- --ephemeral
- --dangerously-bypass-approvals-and-sandbox   (for fully non-interactive edit)
- --skip-git-repo-check (sometimes required)
"""

from __future__ import annotations

import json
import os
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


def _mkstemp_path(suffix: str) -> Path:
    """Create a unique temp file (race-free, unlike the deprecated mktemp)
    and return its path with the fd closed."""
    fd, raw_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return Path(raw_path)


class CodexAdapter:
    """Concrete adapter for OpenAI Codex CLI (`codex exec`)."""

    def __init__(self, binary: str = "codex") -> None:
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

        # Physical isolation for inspect mode: run against a throwaway copy so
        # a sandbox regression in codex-cli can never mutate the real tree
        # (the other adapters already do this). --sandbox read-only stays on
        # inside the copy as defense in depth.
        if mode == "inspect":
            workdir = self._create_disposable_copy(original_workdir)
        else:
            workdir = original_workdir

        schema_path: Optional[Path] = None
        last_message_path: Optional[Path] = None
        try:
            cmd: list[str] = [self.binary, "exec"]

            # Working directory
            cmd += ["--cd", str(workdir)]

            # Sandbox policy (the key normalization for Codex)
            if mode == "inspect":
                cmd += ["--sandbox", "read-only"]
            else:
                cmd += ["--sandbox", "workspace-write"]
                # Make it truly non-interactive for edit mode
                cmd += ["--dangerously-bypass-approvals-and-sandbox"]

            # Output format — prefer structured schema when provided
            if schema:
                schema_path = _mkstemp_path(suffix=".schema.json")
                schema_path.write_text(json.dumps(schema, indent=2))
                cmd += ["--output-schema", str(schema_path)]
            else:
                # Per-invocation last-message file so we can capture clean
                # text; a shared path here would collide across concurrent
                # runs.
                last_message_path = _mkstemp_path(suffix=".codex-last-message.txt")
                cmd += ["--output-last-message", str(last_message_path)]

            cmd += ["--json", "--ephemeral", "--skip-git-repo-check"]

            if model:
                cmd += ["--model", model]

            # The actual prompt must be the last argument
            cmd.append(task)

            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=workdir,
                )
                exit_code = proc.returncode
                stdout = proc.stdout or ""
                stderr = proc.stderr or ""

                parsed_summary = extract_validated_json(stdout, schema)
                if schema and not parsed_summary and exit_code == 0:
                    repair_cmd = list(cmd)
                    repair_cmd[-1] = build_repair_prompt(task, schema, stdout)
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
            except subprocess.TimeoutExpired as e:
                raise WorkerTimeout(f"Codex timed out after {timeout}s") from e
            except Exception as e:
                raise WorkerInvocationError(f"Failed to invoke Codex: {e}") from e

            summary = (
                parsed_summary
                or self._read_last_message(last_message_path)
                or self._extract_summary(stdout, stderr)
            )

            changed_files: list[str] = []
            # Codex JSONL events can contain file writes — best effort parse
            for line in stdout.splitlines():
                if '"type":"file_write"' in line or '"type":"edit"' in line:
                    try:
                        evt = json.loads(line)
                        if "path" in evt:
                            changed_files.append(evt["path"])
                    except Exception:
                        pass

            return CoderResult(
                summary=summary,
                changed_files=sorted(set(changed_files)),
                tests_passed=False,  # Caller decides after running tests
                raw_output=stdout,
                exit_code=exit_code,
                error=stderr if exit_code != 0 else None,
            )
        finally:
            if schema_path:
                schema_path.unlink(missing_ok=True)
            if last_message_path:
                last_message_path.unlink(missing_ok=True)
            if mode == "inspect":
                self._cleanup_disposable(workdir)

    def _read_last_message(self, path: Optional[Path]) -> str:
        if path is None:
            return ""
        try:
            return path.read_text().strip()
        except OSError:
            return ""

    def _create_disposable_copy(self, src: Path) -> Path:
        """
        Create a clean, throwaway copy of the repo for inspect mode.
        Using a plain copytree is the safest portable approach (no git index
        pollution). A real git worktree would also work; copytree is simpler
        and always clean.
        """
        tmp_root = Path(tempfile.mkdtemp(prefix="codex-inspect-"))
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
        if path.exists() and "codex-inspect-" in str(path.parent):
            shutil.rmtree(path, ignore_errors=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass

    def _extract_summary(self, stdout: str, stderr: str) -> str:
        # Try to pull the last human-readable message
        lines = [line for line in (stdout + "\n" + stderr).splitlines() if line.strip()]
        for line in reversed(lines[-20:]):
            if not line.strip().startswith("{"):
                return line.strip()
        return (stdout or stderr or "Codex completed").strip()[:500]
