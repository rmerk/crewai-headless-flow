"""Declarative FlowDefinition topology twin (Phase 2).

Loads ``config/flow.yaml`` (``schema: crewai.flow/v1``) and can rebind a
``CrewAIHeadlessFlow`` instance to stage ``execute_*`` callables so tests
prove kickoff equivalence against the decorator Flow.

CLI / ``run_headless_flow`` still use the decorator class until Phase 3.
"""

from __future__ import annotations

from pathlib import Path

from crewai.flow.flow_definition import FlowDefinition

from .config import DEFAULT_CONFIG_DIR, FlowConfig
from .flow import CrewAIHeadlessFlow
from .run_store import RunStore

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


def build_topology_twin_flow(
    *,
    config: FlowConfig | None = None,
    run_store: RunStore | None = None,
    config_dir: Path | str | None = None,
) -> CrewAIHeadlessFlow:
    """Build a ``CrewAIHeadlessFlow`` whose ``_methods`` come from ``flow.yaml``.

    Constructs the class normally (workers/HITL/verify helpers intact), then
    rebinds stage bodies from the declarative definition. Used by Phase 2
    equivalence tests; not the CLI entrypoint.
    """
    flow = CrewAIHeadlessFlow(config=config, run_store=run_store)
    definition = load_flow_definition(config_dir=config_dir)
    flow._definition = definition
    flow._methods = flow._action_bound_methods()
    for name, method in flow._methods.items():
        setattr(flow, name, method)
    flow._skip_auto_memory = True
    flow.suppress_flow_events = True
    if definition.config.max_method_calls is not None:
        flow.max_method_calls = definition.config.max_method_calls
    return flow
