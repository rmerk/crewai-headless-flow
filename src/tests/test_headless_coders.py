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

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from crewai_headless_flow.workers.base import (
    CoderResult,
    WorkerInvocationError,
    WorkerTimeout,
)
from crewai_headless_flow.workers.claude import ClaudeAdapter
from crewai_headless_flow.workers.codex import CodexAdapter
from crewai_headless_flow.workers.cursor import CursorAdapter
from crewai_headless_flow.workers.gemini import GeminiAdapter
from crewai_headless_flow.workers.grok import GrokAdapter


pytestmark = pytest.mark.offline


# =============================================================================
# CodexAdapter argv tests
# =============================================================================


def test_codex_inspect_mode_uses_read_only_sandbox():
    adapter = CodexAdapter(binary="codex")
    with (
        patch("subprocess.run") as mock_run,
        patch.object(adapter, "_create_disposable_copy") as mock_copy,
    ):
        mock_copy.return_value = Path("/tmp/codex-inspect-xyz/testrepo")
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


def test_codex_inspect_runs_on_disposable_tree_not_original():
    adapter = CodexAdapter(binary="codex")
    original = Path("/tmp/original-repo")

    with (
        patch("subprocess.run") as mock_run,
        patch.object(adapter, "_create_disposable_copy") as mock_copy,
    ):
        mock_copy.return_value = Path("/tmp/codex-inspect-xyz/original-repo")
        mock_run.return_value.stdout = "ok"
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        adapter.run("review", cwd=original, mode="inspect")

        mock_copy.assert_called_once()
        args = mock_run.call_args[0][0]
        cd_idx = args.index("--cd")
        assert "codex-inspect-" in args[cd_idx + 1]
        assert str(original) not in args


def test_codex_edit_mode_uses_unique_last_message_path():
    adapter = CodexAdapter(binary="codex")
    paths: list[str] = []

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = "ok"
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        for _ in range(2):
            adapter.run("do something", cwd="/tmp/testrepo", mode="edit")
            args = mock_run.call_args[0][0]
            idx = args.index("--output-last-message")
            paths.append(args[idx + 1])

    assert "/tmp/codex_last_message.txt" not in paths
    assert paths[0] != paths[1]
    # The per-invocation files are unlinked after the run.
    assert not Path(paths[0]).exists()
    assert not Path(paths[1]).exists()


def test_codex_reads_back_last_message_file_as_summary_fallback():
    adapter = CodexAdapter(binary="codex")

    def fake_run(cmd, **kwargs):
        idx = cmd.index("--output-last-message")
        Path(cmd[idx + 1]).write_text("clean final message\n")
        return type(
            "P", (), {"stdout": '{"type":"noise"}', "stderr": "", "returncode": 0}
        )()

    with patch("subprocess.run", side_effect=fake_run):
        result = adapter.run("do something", cwd="/tmp/testrepo", mode="edit")

    assert result.summary == "clean final message"


def test_codex_disposable_copy_does_not_preserve_absolute_symlink(tmp_path):
    adapter = CodexAdapter(binary="codex")
    outside = tmp_path / "outside.txt"
    outside.write_text("original")

    src = tmp_path / "source-repo"
    src.mkdir()
    (src / "absolute-link.txt").symlink_to(outside)
    (src / "real.txt").write_text("real")

    disposable = adapter._create_disposable_copy(src)

    assert not (disposable / "absolute-link.txt").exists()
    assert (disposable / "real.txt").read_text() == "real"
    assert "codex-inspect-" in str(disposable.parent)

    adapter._cleanup_disposable(disposable)
    assert not disposable.exists()


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


def test_codex_does_one_repair_retry_on_structured_failure():
    adapter = CodexAdapter(binary="codex")
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }

    call_count = {"n": 0}

    def fake_run(cmd, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return type(
                "P", (), {"stdout": "not valid json", "stderr": "", "returncode": 0}
            )()
        return type(
            "P",
            (),
            {"stdout": '{"result": {"ok": true}}', "stderr": "", "returncode": 0},
        )()

    with patch("subprocess.run", side_effect=fake_run):
        result = adapter.run("task", cwd="/tmp/r", mode="edit", schema=schema)

    assert call_count["n"] == 2
    assert '"ok": true' in result.summary


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


def test_claude_relative_cwd_is_normalized_to_absolute_path(tmp_path, monkeypatch):
    adapter = ClaudeAdapter(binary="claude")
    monkeypatch.chdir(tmp_path)

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = '{"result": "done"}'
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        adapter.run("edit the code", cwd=Path("relative-repo"), mode="edit")

        used_cwd = Path(mock_run.call_args.kwargs["cwd"])
        assert used_cwd.is_absolute()
        assert used_cwd.name == "relative-repo"


def test_claude_disposable_copy_cleans_temp_root_when_copytree_fails(tmp_path):
    adapter = ClaudeAdapter(binary="claude")
    src = tmp_path / "source-repo"
    tmp_root = tmp_path / "claude-inspect-known"

    def fake_mkdtemp(prefix: str) -> str:
        assert prefix == "claude-inspect-"
        tmp_root.mkdir()
        return str(tmp_root)

    with (
        patch("tempfile.mkdtemp", side_effect=fake_mkdtemp),
        patch("shutil.copytree", side_effect=OSError("copy failed")),
    ):
        with pytest.raises(OSError, match="copy failed"):
            adapter._create_disposable_copy(src)

    assert not tmp_root.exists()


def test_claude_disposable_copy_does_not_preserve_absolute_symlink(tmp_path):
    adapter = ClaudeAdapter(binary="claude")
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("secret")

    src = tmp_path / "source-repo"
    src.mkdir()
    (src / "absolute-link.txt").symlink_to(outside)
    (src / "absolute-dir-link").symlink_to(outside_dir)

    disposable = adapter._create_disposable_copy(src)

    assert not (disposable / "absolute-link.txt").exists()
    assert not (disposable / "absolute-dir-link").exists()
    assert outside.read_text() == "original"

    adapter._cleanup_disposable(disposable)


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


def test_claude_does_one_repair_retry_on_structured_failure():
    adapter = ClaudeAdapter(binary="claude")
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }

    call_count = {"n": 0}

    def fake_run(cmd, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return type(
                "P",
                (),
                {"stdout": '{"result": "not json"}', "stderr": "", "returncode": 0},
            )()
        return type(
            "P",
            (),
            {"stdout": '{"result": {"ok": true}}', "stderr": "", "returncode": 0},
        )()

    with patch("subprocess.run", side_effect=fake_run):
        result = adapter.run("task", cwd="/tmp/r", mode="edit", schema=schema)

    assert call_count["n"] == 2
    assert '"ok": true' in result.summary


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


def test_claude_passes_effort_when_given():
    adapter = ClaudeAdapter(binary="claude", effort="high")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = '{"result": "ok"}'
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        adapter.run("task", cwd="/tmp/r", mode="edit")

        args = mock_run.call_args[0][0]
        assert "--effort" in args
        effort_idx = args.index("--effort")
        assert args[effort_idx + 1] == "high"


def test_grok_disposable_copy_does_not_preserve_absolute_symlink(tmp_path):
    adapter = GrokAdapter(binary="grok")
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("secret")

    src = tmp_path / "source-repo"
    src.mkdir()
    (src / "absolute-link.txt").symlink_to(outside)
    (src / "absolute-dir-link").symlink_to(outside_dir)

    disposable = adapter._create_disposable_copy(src)

    assert not (disposable / "absolute-link.txt").exists()
    assert not (disposable / "absolute-dir-link").exists()
    assert outside.read_text() == "original"

    adapter._cleanup_disposable(disposable)


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


# =============================================================================
# GeminiAdapter argv + safety tests
# =============================================================================


def test_gemini_inspect_mode_uses_disposable_copy_and_plan_approval():
    adapter = GeminiAdapter(binary="gemini")
    original = Path("/tmp/original-repo").resolve(strict=False)

    with (
        patch("subprocess.run") as mock_run,
        patch.object(adapter, "_create_disposable_copy") as mock_copy,
        patch.object(adapter, "_cleanup_disposable") as mock_cleanup,
    ):
        disposable = Path("/tmp/gemini-inspect-xyz/original-repo")
        mock_copy.return_value = disposable
        mock_run.return_value.stdout = '{"response": "reviewed"}'
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        adapter.run("review this", cwd=original, mode="inspect")

        mock_copy.assert_called_once_with(original)
        mock_cleanup.assert_called_once_with(disposable)
        args = mock_run.call_args[0][0]
        assert args[0] == "gemini"
        assert "--prompt" in args
        assert "--output-format" in args
        assert "json" in args
        assert "--approval-mode" in args
        approval_idx = args.index("--approval-mode")
        assert args[approval_idx + 1] == "plan"
        assert "--skip-trust" in args
        assert mock_run.call_args.kwargs["cwd"] == disposable


def test_gemini_edit_mode_uses_real_cwd_and_yolo_approval():
    adapter = GeminiAdapter(binary="gemini")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = '{"response": "done"}'
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        adapter.run("edit the code", cwd="/tmp/testrepo", mode="edit")

        args = mock_run.call_args[0][0]
        assert "--approval-mode" in args
        approval_idx = args.index("--approval-mode")
        assert args[approval_idx + 1] == "yolo"
        assert "--skip-trust" in args
        assert mock_run.call_args.kwargs["cwd"] == Path("/tmp/testrepo").resolve(
            strict=False
        )


def test_gemini_passes_model_when_given():
    adapter = GeminiAdapter(binary="gemini")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = '{"response": "ok"}'
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        adapter.run("task", cwd="/tmp/r", mode="edit", model="gemini-2.5-pro")

        args = mock_run.call_args[0][0]
        assert "--model" in args
        model_idx = args.index("--model")
        assert args[model_idx + 1] == "gemini-2.5-pro"


def test_gemini_disposable_copy_does_not_preserve_absolute_symlink(tmp_path):
    adapter = GeminiAdapter(binary="gemini")
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("secret")

    src = tmp_path / "source-repo"
    src.mkdir()
    (src / "absolute-link.txt").symlink_to(outside)
    (src / "absolute-dir-link").symlink_to(outside_dir)

    disposable = adapter._create_disposable_copy(src)

    assert not (disposable / "absolute-link.txt").exists()
    assert not (disposable / "absolute-dir-link").exists()
    assert outside.read_text() == "original"

    adapter._cleanup_disposable(disposable)


def test_gemini_structured_output_injects_schema_and_repairs_once():
    adapter = GeminiAdapter(binary="gemini")
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }

    call_count = {"n": 0}

    def fake_run(cmd, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return type(
                "P",
                (),
                {"stdout": '{"response": "not json"}', "stderr": "", "returncode": 0},
            )()
        return type(
            "P",
            (),
            {"stdout": '{"response": {"ok": true}}', "stderr": "", "returncode": 0},
        )()

    with patch("subprocess.run", side_effect=fake_run):
        result = adapter.run("task", cwd="/tmp/r", mode="edit", schema=schema)

    assert call_count["n"] == 2
    assert '"ok": true' in result.summary


def test_gemini_returns_normalized_coder_result_from_json_response():
    adapter = GeminiAdapter(binary="gemini")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = '{"response": "Gemini changed the files."}'
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        result = adapter.run("task", cwd="/tmp/r", mode="edit")

        assert isinstance(result, CoderResult)
        assert result.exit_code == 0
        assert result.summary == "Gemini changed the files."


def test_gemini_timeout_raises_worker_timeout():
    adapter = GeminiAdapter(binary="gemini")

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gemini", 3)):
        with pytest.raises(WorkerTimeout, match="Gemini timed out after 3s"):
            adapter.run("task", cwd="/tmp/r", mode="edit", timeout=3)


def test_gemini_invocation_error_raises_worker_invocation_error():
    adapter = GeminiAdapter(binary="gemini")

    with patch("subprocess.run", side_effect=OSError("missing binary")):
        with pytest.raises(WorkerInvocationError, match="Failed to invoke Gemini"):
            adapter.run("task", cwd="/tmp/r", mode="edit")


# =============================================================================
# CursorAdapter argv + safety tests
# =============================================================================


def test_cursor_inspect_mode_uses_disposable_copy_and_plan_mode():
    adapter = CursorAdapter(binary="cursor")
    original = Path("/tmp/original-repo").resolve(strict=False)

    with (
        patch.object(adapter, "_run_subprocess") as mock_run,
        patch.object(adapter, "_create_disposable_copy") as mock_copy,
        patch.object(adapter, "_cleanup_disposable") as mock_cleanup,
    ):
        disposable = Path("/tmp/cursor-inspect-xyz/original-repo")
        mock_copy.return_value = disposable
        mock_run.return_value.stdout = (
            '{"type":"result","result":"reviewed","subtype":"success"}'
        )
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        adapter.run("review this", cwd=original, mode="inspect")

        mock_copy.assert_called_once_with(original)
        mock_cleanup.assert_called_once_with(disposable)
        args = mock_run.call_args[0][0]
        assert args[0:3] == ["cursor", "agent", "--print"]
        assert "--output-format" in args
        assert "json" in args
        assert "--plan" in args
        assert "--force" not in args
        assert "--trust" in args
        assert "--workspace" in args
        workspace_idx = args.index("--workspace")
        assert args[workspace_idx + 1] == str(disposable)
        assert args[-1] == "review this"
        assert mock_run.call_args.kwargs["cwd"] == disposable


def test_cursor_edit_mode_uses_real_cwd_and_force_mode():
    adapter = CursorAdapter(binary="cursor")
    repo = Path("/tmp/testrepo").resolve(strict=False)

    with patch.object(adapter, "_run_subprocess") as mock_run:
        mock_run.return_value.stdout = (
            '{"type":"result","result":"done","subtype":"success"}'
        )
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        adapter.run("edit the code", cwd=repo, mode="edit")

        args = mock_run.call_args[0][0]
        assert "--force" in args
        assert "--plan" not in args
        workspace_idx = args.index("--workspace")
        assert args[workspace_idx + 1] == str(repo)
        assert mock_run.call_args.kwargs["cwd"] == repo


def test_cursor_passes_model_when_given():
    adapter = CursorAdapter(binary="cursor")

    with patch.object(adapter, "_run_subprocess") as mock_run:
        mock_run.return_value.stdout = '{"result": "ok"}'
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        adapter.run("task", cwd="/tmp/r", mode="edit", model="composer-2.5")

        args = mock_run.call_args[0][0]
        model_idx = args.index("--model")
        assert args[model_idx + 1] == "composer-2.5"


def test_cursor_uses_file_backed_capture_not_pipes():
    """
    Regression test: the Cursor Agent CLI hangs when given pipe-backed
    stdio while the parent process's own stderr fd is a redirected regular
    file (e.g. pytest's default fd-level capture). `_run_subprocess` must
    back stdout/stderr with real temp files, never `subprocess.PIPE`.
    """
    adapter = CursorAdapter(binary="cursor")

    with patch("subprocess.run") as mock_subprocess_run:
        mock_subprocess_run.return_value = subprocess.CompletedProcess(
            args=["cursor"], returncode=0, stdout=b"", stderr=b""
        )

        adapter._run_subprocess(["cursor", "agent"], cwd=Path("/tmp/r"), timeout=30)

        kwargs = mock_subprocess_run.call_args.kwargs
        assert kwargs["stdout"] != subprocess.PIPE
        assert kwargs["stderr"] != subprocess.PIPE
        assert "capture_output" not in kwargs
        assert hasattr(kwargs["stdout"], "fileno")
        assert hasattr(kwargs["stderr"], "fileno")


def test_cursor_disposable_copy_does_not_preserve_absolute_symlink(tmp_path):
    adapter = CursorAdapter(binary="cursor")
    src = tmp_path / "repo"
    src.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    (src / "absolute-link.txt").symlink_to(outside)
    (src / "sample.txt").write_text("inside")

    disposable = adapter._create_disposable_copy(src)

    assert not (disposable / "absolute-link.txt").exists()
    assert (disposable / "sample.txt").read_text() == "inside"
    assert outside.read_text() == "original"

    adapter._cleanup_disposable(disposable)


def test_cursor_structured_output_injects_schema_and_repairs_once():
    adapter = CursorAdapter(binary="cursor")
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }

    call_count = {"n": 0}

    def fake_run(cmd, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return type(
                "P",
                (),
                {
                    "stdout": '{"type":"result","result":"not json"}',
                    "stderr": "",
                    "returncode": 0,
                },
            )()
        return type(
            "P",
            (),
            {
                "stdout": '{"type":"result","result":"{\\"ok\\": true}"}',
                "stderr": "",
                "returncode": 0,
            },
        )()

    with patch.object(adapter, "_run_subprocess", side_effect=fake_run):
        result = adapter.run("task", cwd="/tmp/r", mode="edit", schema=schema)

    assert call_count["n"] == 2
    assert '"ok": true' in result.summary


def test_cursor_returns_normalized_coder_result_from_json_response():
    adapter = CursorAdapter(binary="cursor")

    with patch.object(adapter, "_run_subprocess") as mock_run:
        mock_run.return_value.stdout = (
            '{"type":"result","result":"Cursor changed the files."}'
        )
        mock_run.return_value.stderr = ""
        mock_run.return_value.returncode = 0

        result = adapter.run("task", cwd="/tmp/r", mode="edit")

        assert isinstance(result, CoderResult)
        assert result.exit_code == 0
        assert result.summary == "Cursor changed the files."


def test_cursor_timeout_raises_worker_timeout():
    adapter = CursorAdapter(binary="cursor")

    with patch.object(
        adapter, "_run_subprocess", side_effect=subprocess.TimeoutExpired("cursor", 3)
    ):
        with pytest.raises(WorkerTimeout, match="Cursor timed out after 3s"):
            adapter.run("task", cwd="/tmp/r", mode="edit", timeout=3)


def test_cursor_invocation_error_raises_worker_invocation_error():
    adapter = CursorAdapter(binary="cursor")

    with patch.object(
        adapter, "_run_subprocess", side_effect=OSError("missing binary")
    ):
        with pytest.raises(WorkerInvocationError, match="Failed to invoke Cursor"):
            adapter.run("task", cwd="/tmp/r", mode="edit")


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


# =============================================================================
# Failure paths: non-zero exits map to failed CoderResults (all adapters),
# and codex/grok raise typed infrastructure errors like the other adapters
# =============================================================================


def test_codex_nonzero_exit_returns_failed_coder_result():
    adapter = CodexAdapter(binary="codex")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = "partial output"
        mock_run.return_value.stderr = "codex: model unavailable"
        mock_run.return_value.returncode = 2

        result = adapter.run("task", cwd="/tmp/r", mode="edit")

        assert result.exit_code == 2
        assert result.error == "codex: model unavailable"
        assert result.success is False
        assert result.tests_passed is False


def test_grok_nonzero_exit_returns_failed_coder_result():
    adapter = GrokAdapter(binary="grok")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "grok: rate limited"
        mock_run.return_value.returncode = 2

        result = adapter.run("task", cwd="/tmp/r", mode="edit")

        assert result.exit_code == 2
        assert result.error == "grok: rate limited"
        assert result.success is False


def test_claude_nonzero_exit_returns_failed_coder_result():
    adapter = ClaudeAdapter(binary="claude")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "claude: authentication failed"
        mock_run.return_value.returncode = 2

        result = adapter.run("task", cwd="/tmp/r", mode="edit")

        assert result.exit_code == 2
        assert result.error == "claude: authentication failed"
        assert result.success is False


def test_gemini_nonzero_exit_returns_failed_coder_result():
    adapter = GeminiAdapter(binary="gemini")
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "gemini: quota exceeded"
        mock_run.return_value.returncode = 2

        result = adapter.run("task", cwd="/tmp/r", mode="edit")

        assert result.exit_code == 2
        assert result.error == "gemini: quota exceeded"
        assert result.success is False


def test_cursor_nonzero_exit_returns_failed_coder_result():
    adapter = CursorAdapter(binary="cursor")
    with patch.object(adapter, "_run_subprocess") as mock_run:
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "cursor: workspace not trusted"
        mock_run.return_value.returncode = 2

        result = adapter.run("task", cwd="/tmp/r", mode="edit")

        assert result.exit_code == 2
        assert result.error == "cursor: workspace not trusted"
        assert result.success is False


def test_codex_timeout_raises_worker_timeout():
    adapter = CodexAdapter(binary="codex")

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 3)):
        with pytest.raises(WorkerTimeout, match="Codex timed out after 3s"):
            adapter.run("task", cwd="/tmp/r", mode="edit", timeout=3)


def test_codex_invocation_error_raises_worker_invocation_error():
    adapter = CodexAdapter(binary="codex")

    with patch("subprocess.run", side_effect=OSError("missing binary")):
        with pytest.raises(WorkerInvocationError, match="Failed to invoke Codex"):
            adapter.run("task", cwd="/tmp/r", mode="edit")


def test_grok_timeout_raises_worker_timeout():
    adapter = GrokAdapter(binary="grok")

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("grok", 3)):
        with pytest.raises(WorkerTimeout, match="Grok timed out after 3s"):
            adapter.run("task", cwd="/tmp/r", mode="edit", timeout=3)


def test_grok_invocation_error_raises_worker_invocation_error():
    adapter = GrokAdapter(binary="grok")

    with patch("subprocess.run", side_effect=OSError("missing binary")):
        with pytest.raises(WorkerInvocationError, match="Failed to invoke Grok"):
            adapter.run("task", cwd="/tmp/r", mode="edit")


# =============================================================================
# Disposable-copy hardening: special files must not crash inspect copies
# =============================================================================


@pytest.mark.parametrize(
    "make_adapter",
    [
        lambda: CodexAdapter(binary="codex"),
        lambda: GrokAdapter(binary="grok"),
        lambda: ClaudeAdapter(binary="claude"),
        lambda: GeminiAdapter(binary="gemini"),
        lambda: CursorAdapter(binary="cursor"),
    ],
    ids=["codex", "grok", "claude", "gemini", "cursor"],
)
def test_disposable_copy_skips_special_files(tmp_path: Path, make_adapter):
    # Real repos contain special files — e.g. git's fsmonitor daemon leaves
    # a .git/fsmonitor--daemon.ipc socket — which crash a naive copytree.
    # Every adapter's inspect-mode copy must share the uncopyable filter.
    import os
    import shutil

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("code\n")
    os.mkfifo(repo / ".git" / "fsmonitor--daemon.ipc")

    adapter = make_adapter()
    copy = adapter._create_disposable_copy(repo)
    try:
        assert (copy / "src" / "app.py").exists()
        assert not (copy / ".git" / "fsmonitor--daemon.ipc").exists()
    finally:
        shutil.rmtree(copy.parent, ignore_errors=True)
