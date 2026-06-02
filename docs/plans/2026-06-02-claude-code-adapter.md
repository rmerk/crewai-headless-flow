# Claude Code Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Claude Code as a third opt-in headless coding worker so `worker: claude` can execute Flow stages through the same `HeadlessCoder.run(...)` contract as Codex and Grok.

**Architecture:** Keep the current CrewAI Flow topology intact: the Flow orchestrates stages, `HeadlessCoderTool` injects skill guidance, and concrete workers translate the normalized task into CLI-specific behavior. Implement Claude as an internal `HeadlessCoder` adapter first, not as a CrewAI `BaseAgentAdapter`; CrewAI-native tool exposure remains through `BaseTool` wrappers such as `HeadlessInspectTool`.

**Tech Stack:** Python 3.12, CrewAI Flow, Pydantic, pytest offline tests, subprocess CLI adapters, Claude Code CLI `claude -p`, YAML stage configuration.

---

## Resolved Design

- Claude Code codes by becoming an opt-in direct worker for stages, especially `do_work`.
- Default checked-in worker behavior stays unchanged.
- Worker choice remains YAML-only for v1.
- `worker: claude` must be configurable per stage.
- Unknown worker names must fail loudly instead of falling back to Codex.
- Claude Code installation/authentication is user-managed, like Codex and Grok.
- Claude model strings pass through exactly as configured in YAML.
- Claude inspect mode uses a disposable filesystem copy plus Claude permission restrictions.
- Claude edit mode runs in the real target repository with non-interactive bypass permissions.
- Claude structured output uses native `--json-schema` when a schema is supplied.
- No structured-output repair retry in v1.
- No live Claude test in CI. Optional live smoke can be documented later.
- Documentation must be updated in the same slice.

## External CLI Contract

Use current Claude Code CLI behavior from official docs:

- Non-interactive mode: `claude -p "query"`.
- JSON output: `--output-format json`.
- Structured output: `--json-schema <schema-json>` in print mode.
- Permissions: `--permission-mode dontAsk` for inspect, `--permission-mode bypassPermissions` for edit.
- Model override: `--model <configured-model>`.
- Working directory: use the subprocess `cwd` parameter; Claude Code does not expose a `--cwd` flag.

## File Structure

- Create: `src/crewai_headless_flow/workers/claude.py`
  - Owns Claude-specific argv construction, inspect disposable copy handling, subprocess invocation, timeout/error normalization, and JSON summary extraction.
- Modify: `src/crewai_headless_flow/workers/__init__.py`
  - Exports `ClaudeAdapter`.
- Modify: `src/crewai_headless_flow/flow.py`
  - Replaces implicit Codex fallback with explicit worker registry: `codex`, `grok`, `claude`.
- Modify: `src/tests/test_headless_coders.py`
  - Adds offline subprocess-mocked tests for Claude argv, mode safety, schema/model flags, output normalization, timeout, and invocation errors.
- Modify: `src/tests/test_config_resolution.py`
  - Updates default valid worker assertion and adds a `worker: claude` YAML resolution test.
- Modify: `src/tests/test_flow_router_and_loop.py`
  - Adds factory-level coverage proving `worker: claude` builds a Claude-backed worker tool and unknown worker names raise `ValueError`.
- Modify: `README.md`
  - Documents opt-in Claude YAML usage and user-managed CLI/auth.
- Modify: `DESIGN.md`
  - Records Claude as the third `HeadlessCoder` adapter and documents inspect/edit safety.
- Modify: `AGENTS.md`
  - Moves Claude Code adapter from highest-priority future work to implemented capability; refreshes next opportunities.

---

### Task 1: Add Failing Claude Adapter Tests

**Files:**
- Modify: `src/tests/test_headless_coders.py`

- [ ] **Step 1: Add imports for Claude adapter and worker errors**

Add these imports near the current worker imports:

```python
import subprocess

from crewai_headless_flow.workers.base import (
    CoderResult,
    WorkerInvocationError,
    WorkerTimeout,
)
from crewai_headless_flow.workers.claude import ClaudeAdapter
```

Replace the existing single-line `CoderResult` import with the multi-line import above.

- [ ] **Step 2: Add Claude inspect/edit argv tests**

Append this section before `# Result normalization`:

```python
# =============================================================================
# ClaudeAdapter argv + safety tests
# =============================================================================


def test_claude_inspect_mode_uses_disposable_copy_and_dontask_permissions():
    adapter = ClaudeAdapter(binary="claude")
    original = Path("/tmp/original-repo")

    with (
        patch("subprocess.run") as mock_run,
        patch.object(adapter, "_create_disposable_copy") as mock_copy,
        patch.object(adapter, "_cleanup_disposable") as mock_cleanup,
    ):
        disposable = Path("/tmp/claude-inspect-xyz/original-repo")
        mock_copy.return_value = disposable
        mock_run.return_value.stdout = '{"result": "reviewed"}'
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        adapter.run("review this", cwd=original, mode="inspect")

        mock_copy.assert_called_once_with(original)
        mock_cleanup.assert_called_once_with(disposable)
        args = mock_run.call_args[0][0]
        assert args[0] == "claude"
        assert "-p" in args
        assert "--output-format" in args
        assert "json" in args
        assert "--permission-mode" in args
        permission_idx = args.index("--permission-mode")
        assert args[permission_idx + 1] == "dontAsk"
        assert "--cwd" not in args
        assert mock_run.call_args.kwargs["cwd"] == disposable


def test_claude_edit_mode_uses_real_cwd_and_bypass_permissions():
    adapter = ClaudeAdapter(binary="claude")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = '{"result": "done"}'
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        adapter.run("edit the code", cwd="/tmp/testrepo", mode="edit")

        args = mock_run.call_args[0][0]
        assert "--permission-mode" in args
        permission_idx = args.index("--permission-mode")
        assert args[permission_idx + 1] == "bypassPermissions"
        assert "--cwd" not in args
        assert mock_run.call_args.kwargs["cwd"] == Path("/tmp/testrepo")
```

- [ ] **Step 3: Add Claude schema and model tests**

Append after the edit-mode test:

```python
def test_claude_passes_json_schema_when_given():
    adapter = ClaudeAdapter(binary="claude")
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
    }

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = '{"result": {"summary": "ok"}}'
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        adapter.run("task", cwd="/tmp/r", mode="edit", schema=schema)

        args = mock_run.call_args[0][0]
        assert "--json-schema" in args
        schema_idx = args.index("--json-schema")
        assert '"summary"' in args[schema_idx + 1]


def test_claude_passes_model_when_given():
    adapter = ClaudeAdapter(binary="claude")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = '{"result": "ok"}'
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        adapter.run("task", cwd="/tmp/r", mode="edit", model="sonnet")

        args = mock_run.call_args[0][0]
        assert "--model" in args
        model_idx = args.index("--model")
        assert args[model_idx + 1] == "sonnet"
```

- [ ] **Step 4: Add Claude result/error tests**

Append near the existing result-normalization tests:

```python
def test_claude_returns_normalized_coder_result_from_json_result_string():
    adapter = ClaudeAdapter(binary="claude")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = '{"result": "Claude changed the files."}'
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        result = adapter.run("task", cwd="/tmp/r", mode="edit")

        assert isinstance(result, CoderResult)
        assert result.exit_code == 0
        assert result.summary == "Claude changed the files."
        assert result.raw_output == '{"result": "Claude changed the files."}'


def test_claude_returns_normalized_coder_result_from_structured_result():
    adapter = ClaudeAdapter(binary="claude")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = '{"result": {"summary": "Structured ok"}}'
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        result = adapter.run("task", cwd="/tmp/r", mode="edit")

        assert result.exit_code == 0
        assert '"summary": "Structured ok"' in result.summary


def test_claude_timeout_raises_worker_timeout():
    adapter = ClaudeAdapter(binary="claude")

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 3)):
        with pytest.raises(WorkerTimeout, match="Claude timed out after 3s"):
            adapter.run("task", cwd="/tmp/r", mode="edit", timeout=3)


def test_claude_invocation_error_raises_worker_invocation_error():
    adapter = ClaudeAdapter(binary="claude")

    with patch("subprocess.run", side_effect=OSError("missing binary")):
        with pytest.raises(WorkerInvocationError, match="Failed to invoke Claude"):
            adapter.run("task", cwd="/tmp/r", mode="edit")
```

- [ ] **Step 5: Run the focused tests and verify they fail for missing implementation**

Run:

```bash
uv run pytest -m offline src/tests/test_headless_coders.py -q
```

Expected: FAIL during import with `ModuleNotFoundError: No module named 'crewai_headless_flow.workers.claude'`.

---

### Task 2: Implement `ClaudeAdapter`

**Files:**
- Create: `src/crewai_headless_flow/workers/claude.py`

- [ ] **Step 1: Add the adapter file**

Create `src/crewai_headless_flow/workers/claude.py`:

```python
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
    sanitize_cwd,
)


class ClaudeAdapter:
    """Concrete adapter for Claude Code CLI (`claude`)."""

    def __init__(self, binary: str = "claude") -> None:
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

            return CoderResult(
                summary=self._extract_summary(stdout, stderr),
                changed_files=[],
                tests_passed=False,
                raw_output=stdout,
                exit_code=proc.returncode,
                error=stderr if proc.returncode != 0 else None,
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
        return cmd

    def _create_disposable_copy(self, src: Path) -> Path:
        tmp_root = Path(tempfile.mkdtemp(prefix="claude-inspect-"))
        dst = tmp_root / src.name
            shutil.copytree(
                src,
                dst,
                symlinks=False,
                ignore=self._ignore_symlinks,
                ignore_dangling_symlinks=True,
            )
        return dst

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

        lines = [line.strip() for line in (stdout + "\n" + stderr).splitlines() if line.strip()]
        return lines[-1][:600] if lines else "Claude completed"
```

- [ ] **Step 2: Run the focused adapter tests**

Run:

```bash
uv run pytest -m offline src/tests/test_headless_coders.py -q
```

Expected: PASS for the new Claude tests and existing Codex/Grok tests. If formatting-only failures appear, run `uv run ruff format src/crewai_headless_flow/workers/claude.py src/tests/test_headless_coders.py`.

---

### Task 3: Export Claude and Add Explicit Worker Registry

**Files:**
- Modify: `src/crewai_headless_flow/workers/__init__.py`
- Modify: `src/crewai_headless_flow/flow.py`
- Modify: `src/tests/test_flow_router_and_loop.py`

- [ ] **Step 1: Export `ClaudeAdapter`**

Update `src/crewai_headless_flow/workers/__init__.py` to:

```python
"""
Pluggable headless coding workers.

Public API:
    from crewai_headless_flow.workers import CodexAdapter, GrokAdapter, ClaudeAdapter, CoderResult, ReviewResult
"""

from .base import CoderResult, HeadlessCoder, ReviewResult
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .grok import GrokAdapter

__all__ = [
    "CoderResult",
    "ReviewResult",
    "HeadlessCoder",
    "CodexAdapter",
    "GrokAdapter",
    "ClaudeAdapter",
]
```

- [ ] **Step 2: Update failing Flow registry tests**

Add these imports near the top of `src/tests/test_flow_router_and_loop.py`:

```python
import yaml

from crewai_headless_flow.config import load_config
from crewai_headless_flow.workers import ClaudeAdapter
```

Add these tests near the existing worker/setup tests:

```python
def test_flow_builds_claude_worker_from_worker_yaml(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["do_work"]["worker"] = "claude"
    worker_file.write_text(yaml.safe_dump(data))

    flow = CrewAIHeadlessFlow(config=load_config(sample_config_dir))

    worker_tool = flow._get_worker("do_work")
    assert isinstance(worker_tool.worker, ClaudeAdapter)


def test_flow_rejects_unknown_worker_name(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["do_work"]["worker"] = "claude-typo"
    worker_file.write_text(yaml.safe_dump(data))

    with pytest.raises(
        ValueError,
        match="Unsupported worker 'claude-typo' configured for stage 'do_work'",
    ):
        CrewAIHeadlessFlow(config=load_config(sample_config_dir))
```

If `sample_config_dir` is not available in this test module, create a local fixture by copying the one from `src/tests/test_config_resolution.py` exactly.

- [ ] **Step 3: Make `flow.py` use an explicit registry**

Change imports in `src/crewai_headless_flow/flow.py` from:

```python
from .workers import CodexAdapter, GrokAdapter
```

to:

```python
from .workers import ClaudeAdapter, CodexAdapter, GrokAdapter
```

Add this module-level registry after imports:

```python
WORKER_ADAPTERS: dict[str, type[HeadlessCoder]] = {
    "codex": CodexAdapter,
    "grok": GrokAdapter,
    "claude": ClaudeAdapter,
}
```

Replace the adapter selection in `_setup_workers()` with:

```python
            adapter_cls = WORKER_ADAPTERS.get(stage_cfg.worker)
            if adapter_cls is None:
                supported = ", ".join(sorted(WORKER_ADAPTERS))
                raise ValueError(
                    f"Unsupported worker '{stage_cfg.worker}' configured for stage "
                    f"'{stage}'. Supported workers: {supported}"
                )
            base_worker = adapter_cls()
```

- [ ] **Step 4: Run focused Flow tests**

Run:

```bash
uv run pytest -m offline src/tests/test_flow_router_and_loop.py -q
```

Expected: PASS.

---

### Task 4: Add Config Coverage for `worker: claude`

**Files:**
- Modify: `src/tests/test_config_resolution.py`

- [ ] **Step 1: Update default valid worker assertion**

Change:

```python
        assert resolved.worker in {"codex", "grok"}
```

to:

```python
        assert resolved.worker in {"codex", "grok", "claude"}
```

- [ ] **Step 2: Add a YAML resolution test**

Add this test after `test_worker_switch_changes_resolution_without_code_change`:

```python
def test_worker_yaml_can_select_claude_without_code_change(sample_config_dir: Path):
    worker_file = sample_config_dir / "worker.yaml"
    data = yaml.safe_load(worker_file.read_text())
    data["stages"]["do_work"] = {
        "worker": "claude",
        "model": "sonnet",
        "timeout": 450,
    }
    worker_file.write_text(yaml.safe_dump(data))

    cfg = load_config(sample_config_dir)
    do_work = cfg.get_stage("do_work")

    assert do_work.worker == "claude"
    assert do_work.model == "sonnet"
    assert do_work.timeout == 450
```

- [ ] **Step 3: Run focused config tests**

Run:

```bash
uv run pytest -m offline src/tests/test_config_resolution.py -q
```

Expected: PASS.

---

### Task 5: Documentation Updates

**Files:**
- Modify: `README.md`
- Modify: `DESIGN.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Update README with opt-in Claude example**

Find the worker configuration section in `README.md`. Add this example:

```markdown
To opt into Claude Code for a stage, edit `config/worker.yaml`:

```yaml
stages:
  do_work:
    worker: claude
    model: sonnet
    timeout: 300
```

Claude Code is a user-managed CLI dependency. Install and authenticate the `claude`
binary before live runs; offline tests mock the CLI and do not require Claude Code.
```
```

- [ ] **Step 2: Update DESIGN with Claude adapter behavior**

Add a short subsection near the existing adapter discussion:

```markdown
### Claude Code Adapter

`ClaudeAdapter` is the third built-in `HeadlessCoder` implementation. It uses
Claude Code print mode (`claude -p`) with JSON output, passes `--json-schema`
when structured output is requested, and passes model names through unchanged.

Inspect mode uses a disposable filesystem copy plus `--permission-mode dontAsk`
so review stages cannot mutate the caller's repository. Edit mode runs in the
real target repository with `--permission-mode bypassPermissions`, matching the
non-interactive edit-stage contract used by the other production workers.
```

- [ ] **Step 3: Update AGENTS future work**

In `AGENTS.md`, replace the prioritized opportunity row that currently says Claude Code adapter is the highest priority with a note that it is implemented. Keep stronger structured output as the likely next high-leverage move.

Use wording like:

```markdown
| **Implemented** | **Claude Code adapter** | Validates the pluggable workers architecture with a third production adapter. | Done |
| **Highest** | **Strengthen structured output** | Grok adapter has a basic repair retry. Make it more robust and consistent across workers. | Medium |
```

- [ ] **Step 4: Run docs grep sanity checks**

Run:

```bash
rg -n "ClaudeAdapter|worker: claude|Claude Code adapter|structured output" README.md DESIGN.md AGENTS.md
```

Expected: matches in all three docs.

---

### Task 6: Full Verification and Cleanup

**Files:**
- Potential formatting changes from `ruff format`.

- [ ] **Step 1: Run full offline suite**

Run:

```bash
uv run pytest -m offline
```

Expected: all tests pass.

- [ ] **Step 2: Run lint**

Run:

```bash
uv run ruff check .
```

Expected: no lint errors.

- [ ] **Step 3: Run format check**

Run:

```bash
uv run ruff format --check .
```

Expected: all files already formatted. If it fails, run:

```bash
uv run ruff format .
uv run ruff format --check .
```

- [ ] **Step 4: Run type check**

Run:

```bash
uv run mypy src
```

Expected: success. The existing informational note about untyped test function bodies is acceptable only if mypy exits successfully.

- [ ] **Step 5: Check git diff**

Run:

```bash
git status --short
git diff --check
```

Expected: only planned files changed; `git diff --check` clean.

---

## Self-Review

- Spec coverage: The plan covers Claude as opt-in direct coding worker, YAML selection, inspect/edit safety, structured output, model pass-through, docs, and offline tests.
- Placeholder scan: No implementation step uses `TBD`, `TODO`, or unspecified "write tests" language; each test/implementation step includes concrete code or exact commands.
- Type consistency: `ClaudeAdapter.run(...)` matches `HeadlessCoder.run(...)`; tests use `CoderResult`, `WorkerTimeout`, and `WorkerInvocationError` from `workers.base`; Flow registry maps string worker names to adapter classes.

## Execution Options

1. **Subagent-Driven (recommended):** dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution:** execute tasks in this session using `superpowers:executing-plans`, with checkpoints after focused test groups.
