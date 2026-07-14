"""Shared helpers for FakeFlow monkeypatches after the Phase 3 entrypoint flip."""

from __future__ import annotations

from typing import Any, Callable


def patch_build_headless_flow(
    monkeypatch: Any,
    flow_module: Any,
    make_flow: Callable[..., Any],
) -> None:
    """Route ``build_headless_flow`` to ``make_flow(config=..., run_store=...)``.

    ``make_flow`` may be a FakeFlow class or a factory that returns an instance
    when called with ``config`` / ``run_store``.
    """

    def _fake_build(
        *,
        config: Any = None,
        run_store: Any = None,
        config_dir: Any = None,
    ) -> Any:
        return make_flow(config=config, run_store=run_store)

    monkeypatch.setattr(flow_module, "build_headless_flow", _fake_build)
