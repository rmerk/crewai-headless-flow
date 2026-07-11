from __future__ import annotations

import os
import shutil

import pytest

from crewai_headless_flow.workers.grok import GrokAdapter


pytestmark = pytest.mark.live_grok


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_GROK") != "1",
    reason="Set RUN_LIVE_GROK=1 to enable live Grok smoke tests.",
)
def test_live_grok_inspect_smoke(tmp_path):
    if shutil.which("grok") is None:
        pytest.skip("Grok CLI not installed")
    if not os.getenv("XAI_API_KEY"):
        pytest.skip("XAI_API_KEY not set")

    repo = tmp_path / "repo"
    repo.mkdir()
    sample = repo / "sample.txt"
    sample.write_text("hello\n")

    adapter = GrokAdapter(binary="grok")
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
