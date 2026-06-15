from __future__ import annotations

import os
import shutil

import pytest

from crewai_headless_flow.workers.claude import ClaudeAdapter


pytestmark = pytest.mark.live_claude


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_CLAUDE") != "1",
    reason="Set RUN_LIVE_CLAUDE=1 to enable live Claude smoke tests.",
)
def test_live_claude_inspect_smoke(tmp_path):
    if shutil.which("claude") is None:
        pytest.skip("Claude CLI not installed")

    repo = tmp_path / "repo"
    repo.mkdir()
    sample = repo / "sample.txt"
    sample.write_text("hello\n")

    adapter = ClaudeAdapter(binary="claude")
    result = adapter.run(
        "Inspect this repository and reply with one short sentence mentioning sample.txt.",
        cwd=repo,
        mode="inspect",
        timeout=90,
    )

    assert result.success
    assert result.summary
    assert "sample.txt" in (result.summary + result.raw_output)
    assert sample.read_text() == "hello\n"
