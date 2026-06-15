from __future__ import annotations

import os
import shutil

import pytest

from crewai_headless_flow.workers.gemini import GeminiAdapter


pytestmark = pytest.mark.live_gemini


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_GEMINI") != "1",
    reason="Set RUN_LIVE_GEMINI=1 to enable live Gemini smoke tests.",
)
def test_live_gemini_inspect_smoke(tmp_path):
    if shutil.which("gemini") is None:
        pytest.skip("Gemini CLI not installed")

    repo = tmp_path / "repo"
    repo.mkdir()
    sample = repo / "sample.txt"
    sample.write_text("hello\n")

    adapter = GeminiAdapter(binary="gemini")
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
