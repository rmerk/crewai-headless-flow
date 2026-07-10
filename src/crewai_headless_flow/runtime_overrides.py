"""Shared runtime override parsing for run-time and doctor-time config resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import yaml

from .config import (
    DEFAULT_DELIVER,
    DEFAULT_VERIFY,
    HUMAN_FEEDBACK_BOOLEAN_KEYS,
    FlowConfig,
    _validate_deliver,
    _validate_human_feedback,
    _validate_value_against_schema,
    _validate_verify,
    get_stage_extra_schema,
    load_config,
)
from .human_feedback_actions import (
    resolve_human_feedback_action_stage,
    supported_human_feedback_action_targets,
    supported_human_feedback_actions,
)


def load_runtime_config(
    *,
    config_dir: Path | None = None,
    skill_overrides: list[str] | None = None,
    default_worker_overrides: list[str] | None = None,
    default_model_overrides: list[str] | None = None,
    default_timeout_overrides: list[str] | None = None,
    worker_overrides: list[str] | None = None,
    model_overrides: list[str] | None = None,
    timeout_overrides: list[str] | None = None,
    stage_extra_overrides: list[str] | None = None,
    human_feedback_overrides: list[str] | None = None,
    human_feedback_action_overrides: list[str] | None = None,
    deliver_overrides: list[str] | None = None,
    verify_overrides: list[str] | None = None,
) -> FlowConfig:
    config = load_config(config_dir)
    apply_skill_overrides(config, skill_overrides)
    apply_default_overrides(config, "worker", default_worker_overrides)
    apply_default_overrides(
        config,
        "model",
        default_model_overrides,
        cast=_parse_model_override,
    )
    apply_default_overrides(
        config,
        "timeout",
        default_timeout_overrides,
        cast=int,
    )
    apply_stage_overrides(config, "worker", worker_overrides)
    apply_stage_overrides(config, "model", model_overrides)
    apply_stage_overrides(config, "timeout", timeout_overrides, cast=int)
    apply_stage_extra_overrides(config, stage_extra_overrides)
    apply_human_feedback_overrides(config, human_feedback_overrides)
    apply_human_feedback_action_overrides(config, human_feedback_action_overrides)
    apply_deliver_overrides(config, deliver_overrides)
    apply_verify_overrides(config, verify_overrides)
    return config


def apply_skill_overrides(config: FlowConfig, overrides: list[str] | None) -> None:
    from .skills.loader import get_default_loader

    loader = get_default_loader()
    for raw in overrides or []:
        if "=" not in raw:
            raise ValueError(
                f"Invalid override '{raw}'. Expected STAGE=SKILL for skill."
            )
        stage, value = raw.split("=", 1)
        stage = stage.strip()
        skill_name = value.strip()
        if not stage or not skill_name:
            raise ValueError(
                f"Invalid override '{raw}'. Expected STAGE=SKILL for skill."
            )
        if stage not in config.skills:
            known = ", ".join(config.stages)
            raise ValueError(
                f"Unknown stage '{stage}' in skill override. Known stages: {known}"
            )
        try:
            loader.get_skill(skill_name)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        config.skills[stage] = skill_name
        config._stage_cache.pop(stage, None)


def apply_stage_overrides(
    config: FlowConfig,
    field_name: str,
    overrides: list[str] | None,
    *,
    cast: Callable[[str], object] = str,
) -> None:
    for raw in overrides or []:
        if "=" not in raw:
            raise ValueError(
                f"Invalid override '{raw}'. Expected STAGE=VALUE for {field_name}."
            )
        stage, value = raw.split("=", 1)
        stage = stage.strip()
        value = value.strip()
        if not stage or not value:
            raise ValueError(
                f"Invalid override '{raw}'. Expected STAGE=VALUE for {field_name}."
            )
        if stage not in config.skills:
            known = ", ".join(config.stages)
            raise ValueError(
                f"Unknown stage '{stage}' in {field_name} override. Known stages: {known}"
            )
        config.workers.setdefault(stage, {})[field_name] = cast(value)
        config._stage_cache.pop(stage, None)


def apply_default_overrides(
    config: FlowConfig,
    field_name: str,
    overrides: list[str] | None,
    *,
    cast: Callable[[str], object] = str,
) -> None:
    for raw in overrides or []:
        value = raw.strip()
        if not value:
            raise ValueError(
                f"Invalid override '{raw}'. Expected VALUE for default {field_name}."
            )
        config.defaults[field_name] = cast(value)
        config._stage_cache.clear()


# Top-level human_feedback keys whose value is a nested tree, reachable via a
# dotted override path (e.g. conditional.triggers.repeated_task_failure.min_attempts,
# escalation.channel).
_HUMAN_FEEDBACK_NESTED_ROOTS = {"conditional", "escalation"}


def _set_nested_human_feedback_override(
    config: FlowConfig, key: str, value: str, raw: str
) -> None:
    parts = key.split(".")
    if parts[0] not in _HUMAN_FEEDBACK_NESTED_ROOTS:
        supported = ", ".join(sorted(_HUMAN_FEEDBACK_NESTED_ROOTS))
        raise ValueError(
            f"Unknown human feedback override '{key}'. Dotted overrides are only "
            f"supported under: {supported}."
        )
    if "" in parts:
        raise ValueError(
            f"Invalid override '{raw}'. Empty segment in dotted key '{key}'."
        )
    cursor: dict[str, object] = config.human_feedback
    for part in parts[:-1]:
        nested = cursor.get(part)
        if not isinstance(nested, dict):
            nested = {}
            cursor[part] = nested
        cursor = nested
    cursor[parts[-1]] = parse_override_value(value)


def apply_human_feedback_overrides(
    config: FlowConfig,
    overrides: list[str] | None,
) -> None:
    supported_keys = set(config.human_feedback.keys()) | {"action_allowlist"}
    applied = False
    for raw in overrides or []:
        if "=" not in raw:
            raise ValueError(
                f"Invalid override '{raw}'. Expected KEY=VALUE for human feedback."
            )
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise ValueError(
                f"Invalid override '{raw}'. Expected KEY=VALUE for human feedback."
            )
        if key == "action_allowlist" or key.startswith("action_allowlist."):
            raise ValueError(
                "human_feedback.action_allowlist is stage-scoped. "
                "Use --override-human-feedback-action STAGE=ACTION[,ACTION...] instead."
            )
        if "." in key:
            _set_nested_human_feedback_override(config, key, value, raw)
        elif key not in supported_keys:
            known = ", ".join(sorted(supported_keys))
            raise ValueError(
                f"Unknown human feedback override '{key}'. Supported keys: {known}"
            )
        elif key in HUMAN_FEEDBACK_BOOLEAN_KEYS:
            config.human_feedback[key] = parse_bool(value, key)
        else:
            config.human_feedback[key] = parse_override_value(value)
        applied = True

    # Re-validate so overridden values (mode enum, nested trigger thresholds,
    # stray keys) are schema-checked exactly as file-loaded config would be.
    if applied:
        config.human_feedback = _validate_human_feedback(config.human_feedback)


def apply_deliver_overrides(
    config: FlowConfig,
    overrides: list[str] | None,
) -> None:
    applied = False
    for raw in overrides or []:
        if "=" not in raw:
            raise ValueError(
                f"Invalid override '{raw}'. Expected KEY=VALUE for deliver."
            )
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise ValueError(
                f"Invalid override '{raw}'. Expected KEY=VALUE for deliver."
            )
        if key not in DEFAULT_DELIVER:
            known = ", ".join(sorted(DEFAULT_DELIVER))
            raise ValueError(
                f"Unknown deliver override '{key}'. Supported keys: {known}"
            )
        config.deliver[key] = parse_override_value(value)
        applied = True

    # Re-validate so overridden values are schema-checked exactly as
    # file-loaded config would be.
    if applied:
        config.deliver = _validate_deliver(config.deliver)


def apply_verify_overrides(
    config: FlowConfig,
    overrides: list[str] | None,
) -> None:
    applied = False
    for raw in overrides or []:
        if "=" not in raw:
            raise ValueError(
                f"Invalid override '{raw}'. Expected KEY=VALUE for verify."
            )
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise ValueError(
                f"Invalid override '{raw}'. Expected KEY=VALUE for verify."
            )
        if key not in DEFAULT_VERIFY:
            known = ", ".join(sorted(DEFAULT_VERIFY))
            raise ValueError(
                f"Unknown verify override '{key}'. Supported keys: {known}"
            )
        config.verify[key] = parse_override_value(value)
        applied = True

    # Re-validate so overridden values are schema-checked exactly as
    # file-loaded config would be.
    if applied:
        config.verify = _validate_verify(config.verify)


def apply_human_feedback_action_overrides(
    config: FlowConfig,
    overrides: list[str] | None,
) -> None:
    allowlist = dict(config.human_feedback.get("action_allowlist", {}) or {})
    for raw in overrides or []:
        if "=" not in raw:
            raise ValueError(
                f"Invalid override '{raw}'. Expected STAGE=ACTION[,ACTION...] for human feedback actions."
            )
        target, value = raw.split("=", 1)
        target = target.strip()
        value = value.strip()
        if not target or not value:
            raise ValueError(
                f"Invalid override '{raw}'. Expected TARGET=ACTION[,ACTION...] for human feedback actions."
            )
        resolved_stage = resolve_human_feedback_action_stage(target)
        if resolved_stage is None:
            known = ", ".join(sorted(supported_human_feedback_action_targets()))
            raise ValueError(
                f"Unknown human feedback action target '{target}'. Known targets: {known}"
            )

        supported = set(supported_human_feedback_actions(target))
        if not supported:
            raise ValueError(
                f"Target '{target}' does not support advanced human feedback actions."
            )
        if value.lower() == "none":
            allowlist[target] = []
            continue

        actions = [part.strip() for part in value.split(",") if part.strip()]
        if not actions:
            raise ValueError(
                f"Invalid override '{raw}'. Expected TARGET=ACTION[,ACTION...] for human feedback actions."
            )
        unknown = [action for action in actions if action not in supported]
        if unknown:
            supported_text = ", ".join(sorted(supported))
            unknown_text = ", ".join(unknown)
            raise ValueError(
                f"Unsupported human feedback actions for target '{target}': {unknown_text}. "
                f"Supported actions: {supported_text}"
            )
        allowlist[target] = actions

    config.human_feedback["action_allowlist"] = allowlist


def apply_stage_extra_overrides(
    config: FlowConfig,
    overrides: list[str] | None,
) -> None:
    for raw in overrides or []:
        if "=" not in raw or "." not in raw.split("=", 1)[0]:
            raise ValueError(
                f"Invalid override '{raw}'. Expected STAGE.PATH=VALUE for stage extras."
            )
        stage_path, value_raw = raw.split("=", 1)
        stage, *path_parts = [part.strip() for part in stage_path.split(".")]
        if not stage or not path_parts or any(not part for part in path_parts):
            raise ValueError(
                f"Invalid override '{raw}'. Expected STAGE.PATH=VALUE for stage extras."
            )
        if stage not in config.skills:
            known = ", ".join(config.stages)
            raise ValueError(
                f"Unknown stage '{stage}' in stage extra override. Known stages: {known}"
            )
        if path_parts[0] in {"worker", "model", "timeout"}:
            raise ValueError(
                f"Stage extra override '{raw}' targets '{path_parts[0]}'. "
                f"Use --override-{path_parts[0]} instead."
            )

        schema = get_stage_extra_schema(stage, path_parts)
        value = parse_override_value(value_raw.strip())
        _validate_value_against_schema(
            f"stage override {stage}.{'.'.join(path_parts)}",
            value,
            schema,
        )

        root = config.workers.setdefault(stage, {})
        cursor = root
        for key in path_parts[:-1]:
            nested = cursor.get(key)
            if nested is None:
                nested = {}
                cursor[key] = nested
            elif not isinstance(nested, dict):
                raise ValueError(
                    f"Cannot set nested override '{raw}' because '{key}' is not a mapping."
                )
            cursor = nested

        cursor[path_parts[-1]] = value
        config._stage_cache.pop(stage, None)


def parse_bool(raw: str, key: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"Invalid boolean value '{raw}' for human_feedback.{key}. Use true/false."
    )


def parse_override_value(raw: str):
    return yaml.safe_load(raw)


def _parse_model_override(raw: str) -> str | None:
    parsed = parse_override_value(raw)
    if parsed is None or isinstance(parsed, str):
        return parsed
    value_type = type(parsed).__name__
    raise ValueError(
        f"Invalid model override '{raw}'. Expected string or null, got {value_type}."
    )
