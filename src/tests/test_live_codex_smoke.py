from __future__ import annotations

import os
import shutil

import pytest

from crewai_headless_flow.workers.codex import CodexAdapter


pytestmark = pytest.mark.live_codex


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_CODEX") != "1",
    reason="Set RUN_LIVE_CODEX=1 to enable live Codex smoke tests.",
)
def test_live_codex_inspect_smoke(tmp_path):
    if shutil.which("codex") is None:
        pytest.skip("Codex CLI not installed")

    repo = tmp_path / "repo"
    repo.mkdir()
    sample = repo / "sample.txt"
    sample.write_text("hello\n")

    adapter = CodexAdapter(binary="codex")
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
