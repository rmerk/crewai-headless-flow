from __future__ import annotations

import os
import shutil

import pytest

from crewai_headless_flow.workers.cursor import CursorAdapter


pytestmark = pytest.mark.live_cursor


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_CURSOR") != "1",
    reason="Set RUN_LIVE_CURSOR=1 to enable live Cursor smoke tests.",
)
def test_live_cursor_inspect_smoke(tmp_path):
    if shutil.which("cursor") is None:
        pytest.skip("Cursor CLI not installed")
    if not os.getenv("CURSOR_API_KEY"):
        pytest.skip("CURSOR_API_KEY is not set")

    repo = tmp_path / "repo"
    repo.mkdir()
    sample = repo / "sample.txt"
    sample.write_text("hello\n")

    adapter = CursorAdapter(binary="cursor")
    result = adapter.run(
        "Inspect this repository and reply with one short sentence mentioning sample.txt.",
        cwd=repo,
        mode="inspect",
        timeout=120,
    )

    assert result.success
    assert result.summary
    assert "sample.txt" in (result.summary + result.raw_output)
    assert sample.read_text() == "hello\n"
