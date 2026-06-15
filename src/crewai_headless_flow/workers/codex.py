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
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .base import (
    CoderResult,
    Mode,
    WorkerInvocationError,
    WorkerTimeout,
    sanitize_cwd,
)
from .structured_output import build_repair_prompt, extract_validated_json


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
        workdir = sanitize_cwd(cwd)
        workdir.mkdir(parents=True, exist_ok=True)

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
        schema_path: Optional[Path] = None
        if schema:
            schema_path = Path(tempfile.mktemp(suffix=".schema.json"))
            schema_path.write_text(json.dumps(schema, indent=2))
            cmd += ["--output-schema", str(schema_path)]
        else:
            # Fallback to last-message file so we can capture clean text
            cmd += ["--output-last-message", "/tmp/codex_last_message.txt"]

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
        finally:
            if schema_path and schema_path.exists():
                schema_path.unlink(missing_ok=True)

        summary = parsed_summary or self._extract_summary(stdout, stderr)

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

    def _extract_summary(self, stdout: str, stderr: str) -> str:
        # Try to pull the last human-readable message
        lines = [line for line in (stdout + "\n" + stderr).splitlines() if line.strip()]
        for line in reversed(lines[-20:]):
            if not line.strip().startswith("{"):
                return line.strip()
        return (stdout or stderr or "Codex completed").strip()[:500]
