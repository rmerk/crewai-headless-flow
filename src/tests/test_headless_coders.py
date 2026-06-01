"""
Milestone 2: HeadlessCoder adapters — fully mocked, exact argv assertions.

These tests are the contract that guarantees:
- Codex always gets the right sandbox + bypass flags per mode
- Grok inspect NEVER gets --always-approve and runs on a disposable tree
- Grok edit DOES get --always-approve
- Both adapters produce normalized CoderResult
- Grok structured-output repair path is exercised

Run with: pytest -m offline
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from crewai_headless_flow.workers.base import CoderResult
from crewai_headless_flow.workers.codex import CodexAdapter
from crewai_headless_flow.workers.grok import GrokAdapter


pytestmark = pytest.mark.offline


# =============================================================================
# CodexAdapter argv tests
# =============================================================================


def test_codex_inspect_mode_uses_read_only_sandbox():
    adapter = CodexAdapter(binary="codex")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = "ok"
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        adapter.run("do something", cwd="/tmp/testrepo", mode="inspect")

        args = mock_run.call_args[0][0]
        assert "codex" in args[0]
        assert "exec" in args
        assert "--sandbox" in args
        assert "read-only" in args
        assert "--dangerously-bypass-approvals-and-sandbox" not in args


def test_codex_edit_mode_uses_workspace_write_and_bypass():
    adapter = CodexAdapter(binary="codex")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = "ok"
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        adapter.run("do something", cwd="/tmp/testrepo", mode="edit")

        args = mock_run.call_args[0][0]
        assert "--sandbox" in args
        assert "workspace-write" in args
        assert "--dangerously-bypass-approvals-and-sandbox" in args


def test_codex_passes_output_schema_when_given():
    adapter = CodexAdapter(binary="codex")
    schema = {"type": "object", "properties": {"summary": {"type": "string"}}}

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = "ok"
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        adapter.run("task", cwd="/tmp/r", mode="edit", schema=schema)

        args = mock_run.call_args[0][0]
        assert "--output-schema" in args


# =============================================================================
# GrokAdapter argv + safety tests
# =============================================================================


def test_grok_edit_mode_adds_always_approve():
    adapter = GrokAdapter(binary="grok")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = '{"text": "done"}'
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        adapter.run("edit the code", cwd="/tmp/testrepo", mode="edit")

        args = mock_run.call_args[0][0]
        assert "-p" in args or "--single" in args  # we use -p
        assert "--always-approve" in args
        assert "--cwd" in args


def test_grok_inspect_mode_never_passes_always_approve():
    adapter = GrokAdapter(binary="grok")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = '{"text": "reviewed"}'
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        adapter.run("review this", cwd="/tmp/testrepo", mode="inspect")

        args = mock_run.call_args[0][0]
        assert "--always-approve" not in args
        # It must have used a disposable path, not the original cwd
        cwd_idx = args.index("--cwd") if "--cwd" in args else args.index("--cwd")
        used_cwd = Path(args[cwd_idx + 1])
        assert "grok-inspect-" in str(used_cwd) or used_cwd.name != "testrepo"


def test_grok_inspect_runs_on_disposable_tree_not_original():
    adapter = GrokAdapter(binary="grok")
    original = Path("/tmp/original-repo")

    with (
        patch("subprocess.run") as mock_run,
        patch.object(adapter, "_create_disposable_copy") as mock_copy,
    ):
        mock_copy.return_value = Path("/tmp/grok-inspect-xyz/original-repo")
        mock_run.return_value.stdout = '{"text": "ok"}'
        mock_run.return_value.returncode = 0

        adapter.run("review", cwd=original, mode="inspect")

        mock_copy.assert_called_once()
        # The command must have used the disposable path
        args = mock_run.call_args[0][0]
        assert "/tmp/grok-inspect-" in " ".join(args)


def test_grok_structured_output_injects_schema_into_prompt():
    adapter = GrokAdapter(binary="grok")
    schema = {"type": "object", "properties": {"status": {"type": "string"}}}

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = '{"text": "{\\"status\\": \\"pass\\"}"}'
        mock_run.return_value.returncode = 0

        adapter.run("do review", cwd="/tmp/r", mode="inspect", schema=schema)

        called_task = mock_run.call_args[0][0]
        # The task passed to -p must contain the schema text
        assert "status" in " ".join(called_task)
        assert (
            "Respond with **only**" in " ".join(called_task)
            or "only valid JSON" in " ".join(called_task).lower()
        )


# =============================================================================
# Result normalization
# =============================================================================


def test_codex_returns_normalized_coder_result():
    adapter = CodexAdapter(binary="codex")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = "Changes made to two files."
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        result = adapter.run("task", cwd="/tmp/r", mode="edit")

        assert isinstance(result, CoderResult)
        assert result.exit_code == 0
        assert "Changes made" in result.summary


def test_grok_returns_normalized_coder_result():
    adapter = GrokAdapter(binary="grok")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = '{"text": "All good."}'
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        result = adapter.run("task", cwd="/tmp/r", mode="edit")

        assert isinstance(result, CoderResult)
        assert result.exit_code == 0


# =============================================================================
# Grok repair path (one retry on bad JSON)
# =============================================================================


def test_grok_does_one_repair_retry_on_structured_failure():
    adapter = GrokAdapter(binary="grok")
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}

    call_count = {"n": 0}

    def fake_run(cmd, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call returns garbage JSON
            return type(
                "P", (), {"stdout": "I did the thing", "stderr": "", "returncode": 0}
            )()
        else:
            # Second call returns valid JSON
            return type(
                "P",
                (),
                {
                    "stdout": '{"text": "{\\"ok\\": true}"}',
                    "stderr": "",
                    "returncode": 0,
                },
            )()

    with patch("subprocess.run", side_effect=fake_run):
        result = adapter.run(
            "do it", cwd="/tmp/r", mode="edit", schema=schema, timeout=30
        )

        assert call_count["n"] >= 2
        assert result.exit_code == 0
