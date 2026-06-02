"""Command-line interface for crewai-headless-flow."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from . import diagnostics
from .config import load_config
from .diagnostics import normalize_path, run_doctor, run_preflight


SUBCOMMANDS = {"run", "doctor", "preflight"}


def run_headless_flow(**kwargs):
    from .flow import run_headless_flow as backend

    return backend(**kwargs)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = _build_help_parser()

    if not args or args in (["--help"], ["-h"]):
        parser.print_help()
        return 0

    try:
        if args[0] == "run":
            return _handle_run(_parse_run_args(args[1:]))
        if args[0] == "doctor":
            return _handle_doctor(_parse_doctor_args(args[1:]))
        if args[0] == "preflight":
            return _handle_preflight(_parse_preflight_args(args[1:]))
        if _has_legacy_args(args):
            return _handle_run(_parse_legacy_args(args))
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    parser.print_usage(sys.stderr)
    print(
        f"Error: unknown command or missing --request/--target-repo: {args[0]}",
        file=sys.stderr,
    )
    return 2


def _handle_run(args: argparse.Namespace) -> int:
    target_repo = normalize_path(args.target_repo)
    config_dir = normalize_path(args.config_dir) if args.config_dir else None
    preflight = run_preflight(target_repo, config_dir=config_dir)
    if preflight.status == "fail":
        print(
            "Error: Preflight failed: " + "; ".join(preflight.failures),
            file=sys.stderr,
        )
        return 1
    config = load_config(config_dir) if config_dir else None
    state = run_headless_flow(
        request=args.request,
        target_repo=str(target_repo),
        max_revisions=args.max_revisions,
        config=config,
    )
    return 0 if getattr(state, "status", None) == "completed" else 1


def _handle_doctor(args: argparse.Namespace) -> int:
    report = run_doctor(
        config_dir=args.config_dir,
        target_repo=args.target_repo,
    )
    _print_report(report, args.format)
    return 1 if report.status == "fail" else 0


def _handle_preflight(args: argparse.Namespace) -> int:
    report = run_preflight(args.target_repo, config_dir=args.config_dir)
    _print_report(report, args.format)
    return 1 if report.status == "fail" else 0


def _print_report(report: diagnostics.DiagnosticReport, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return

    for check in report.checks:
        print(f"{check.status.upper():<4} {check.name}: {check.message}")
    print(f"Status: {report.status}")


def _parse_legacy_args(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m crewai_headless_flow")
    _add_run_args(parser)
    parsed, extras = parser.parse_known_args(args)
    if extras:
        parser.error(
            "legacy --request/--target-repo cannot be combined with a subcommand"
        )
    return parsed


def _parse_run_args(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m crewai_headless_flow run")
    _add_run_args(parser)
    return parser.parse_args(args)


def _parse_doctor_args(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m crewai_headless_flow doctor")
    parser.add_argument("--target-repo")
    parser.add_argument("--config-dir")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args(args)


def _parse_preflight_args(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m crewai_headless_flow preflight")
    parser.add_argument("--target-repo", required=True)
    parser.add_argument("--config-dir")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args(args)


def _add_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--request", required=True)
    parser.add_argument("--target-repo", required=True)
    parser.add_argument("--max-revisions", type=int, default=2)
    parser.add_argument("--config-dir")


def _build_help_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m crewai_headless_flow")
    parser.add_argument("--request", help="Legacy run request")
    parser.add_argument("--target-repo", help="Legacy run target repository")
    parser.add_argument("--max-revisions", type=int, default=2)
    parser.add_argument("--config-dir")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("run", help="Run the full headless flow")
    subparsers.add_parser("doctor", help="Run detect-only environment diagnostics")
    subparsers.add_parser("preflight", help="Inspect target repository readiness")
    return parser


def _has_legacy_args(args: list[str]) -> bool:
    return "--request" in args or "--target-repo" in args


if __name__ == "__main__":
    raise SystemExit(main())
