"""Declarative FlowDefinition topology (Phase 3).

Loads and validates ``config/flow.yaml`` (``schema: crewai.flow/v1``). The
library/CLI constructor that binds stage ``execute_*`` callables onto a
``CrewAIHeadlessFlow`` shell lives in ``flow.build_headless_flow``.
"""

from __future__ import annotations

from pathlib import Path

from crewai.flow.flow_definition import FlowDefinition

from .config import DEFAULT_CONFIG_DIR

REQUIRED_METHODS = frozenset(
    {
        "plan",
        "do_work",
        "review",
        "process_revision",
        "finalize",
        "handle_aborted",
        "handle_failed",
    }
)
REQUIRED_REVIEW_EMIT = ("pass", "revise", "aborted")


class IncompleteFlowTopologyError(ValueError):
    """Raised when a present ``flow.yaml`` is missing required methods/emits."""


def resolve_flow_yaml_path(config_dir: Path | str | None = None) -> Path:
    """Resolve ``flow.yaml`` with crew-bundle-style fallback.

    Prefer ``<config_dir>/flow.yaml`` when present. If missing, fall back to
    the default/bundled config pack (ADR-0012). A present but invalid file is
    rejected later by ``load_flow_definition`` (fail closed).
    """
    if config_dir is not None:
        preferred = Path(config_dir) / "flow.yaml"
        if preferred.is_file():
            return preferred

    default = Path(DEFAULT_CONFIG_DIR) / "flow.yaml"
    if default.is_file():
        return default
    raise FileNotFoundError(
        f"Missing flow.yaml under default config pack {DEFAULT_CONFIG_DIR}"
    )


def load_flow_definition(
    config_dir: Path | str | None = None,
) -> FlowDefinition:
    """Load and validate the canonical headless-flow topology declaration."""
    path = resolve_flow_yaml_path(config_dir=config_dir)
    definition = FlowDefinition.from_declaration(path=path)
    _validate_canonical_topology(definition, path=path)
    return definition


def _validate_canonical_topology(definition: FlowDefinition, *, path: Path) -> None:
    missing = REQUIRED_METHODS - set(definition.methods)
    if missing:
        raise IncompleteFlowTopologyError(
            f"Incomplete flow topology in {path}: missing methods {sorted(missing)}"
        )

    review = definition.methods["review"]
    if not review.router:
        raise IncompleteFlowTopologyError(
            f"Incomplete flow topology in {path}: review must be a router"
        )
    emit = list(review.emit or [])
    if emit != list(REQUIRED_REVIEW_EMIT):
        raise IncompleteFlowTopologyError(
            f"Incomplete flow topology in {path}: review.emit must be "
            f"{list(REQUIRED_REVIEW_EMIT)}, got {emit}"
        )

    if definition.methods["plan"].start is not True:
        raise IncompleteFlowTopologyError(
            f"Incomplete flow topology in {path}: plan must be start=true"
        )
