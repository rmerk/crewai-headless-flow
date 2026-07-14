"""Command-line interface for crewai-headless-flow."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Sequence

from . import diagnostics
from .config import DEFAULT_CONFIG_DIR
from .diagnostics import normalize_path, run_doctor, run_preflight
from .human_feedback_actions import human_feedback_target_label
from .runtime_overrides import load_runtime_config
from .state import FlowState


SUBCOMMANDS = {"run", "doctor", "preflight", "enqueue", "serve", "runs", "jobs"}


def run_headless_flow(**kwargs):
    from .flow import run_headless_flow as backend

    return backend(**kwargs)


def resume_headless_flow(**kwargs):
    from .flow import resume_headless_flow as backend

    return backend(**kwargs)


def _configure_logging() -> None:
    """Route the package's diagnostic narration to stdout for CLI runs.

    Deliberately not ``logging.basicConfig``: crewai/litellm configure the
    root logger themselves and would fight it. Only the package logger is
    configured (plain messages, INFO, no propagation), and only once —
    library users who configure ``crewai_headless_flow`` themselves are
    left alone.
    """
    package_logger = logging.getLogger("crewai_headless_flow")
    if package_logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    package_logger.addHandler(handler)
    package_logger.setLevel(logging.INFO)
    package_logger.propagate = False


def main(argv: Sequence[str] | None = None) -> int:
    _configure_logging()
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
        if args[0] == "enqueue":
            return _handle_enqueue(_parse_enqueue_args(args[1:]))
        if args[0] == "serve":
            return _handle_serve(_parse_serve_args(args[1:]))
        if args[0] == "runs":
            return _handle_runs(_parse_runs_args(args[1:]))
        if args[0] == "jobs":
            return _handle_jobs(_parse_jobs_args(args[1:]))
        if args[0] in ("dashboard", "ui"):
            return _handle_dashboard(_parse_dashboard_args(args[1:]))
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
    explicit_config_dir = normalize_path(args.config_dir) if args.config_dir else None
    max_revisions = _resolve_max_revisions(args.max_revisions)

    if args.resume_state_file:
        state = _load_state_file(args.resume_state_file)
        if args.request or args.target_repo:
            raise ValueError(
                "--resume-state-file cannot be combined with --request or --target-repo"
            )
        target_repo = normalize_path(state.target_repo)
        if max_revisions is not None:
            state.max_revisions = max_revisions
    else:
        if not args.request or not args.target_repo:
            raise ValueError(
                "run requires --request and --target-repo unless --resume-state-file is used"
            )
        target_repo = normalize_path(args.target_repo)
        state = None

    config_dir = _resolve_run_config_dir(explicit_config_dir, state)
    config = load_runtime_config(
        config_dir=config_dir,
        skill_overrides=args.override_skill,
        default_worker_overrides=args.override_default_worker,
        default_model_overrides=args.override_default_model,
        default_timeout_overrides=args.override_default_timeout,
        worker_overrides=args.override_worker,
        model_overrides=args.override_model,
        timeout_overrides=args.override_timeout,
        stage_extra_overrides=args.override_stage_extra,
        human_feedback_overrides=args.override_human_feedback,
        human_feedback_action_overrides=args.override_human_feedback_action,
        deliver_overrides=args.override_deliver,
        verify_overrides=args.override_verify,
        worker_binary_overrides=args.override_worker_binary,
    )

    preflight = run_preflight(target_repo, config_dir=config_dir)
    if preflight.status == "fail":
        print(
            "Error: Preflight failed: " + "; ".join(preflight.failures),
            file=sys.stderr,
        )
        return 1

    runs_dir = _resolve_runs_dir(getattr(args, "runs_dir", None))

    if state is not None:
        state.target_repo = str(target_repo)
        resumed = resume_headless_flow(state=state, config=config, runs_dir=runs_dir)
        data = _state_to_dict(resumed)
        result_state = resumed
    else:
        result_state = run_headless_flow(
            request=args.request,
            target_repo=str(target_repo),
            max_revisions=max_revisions or config.max_revisions or 2,
            config=config,
            runs_dir=runs_dir,
            config_dir=config_dir,
        )

    data = _state_to_dict(result_state)
    _attach_config_dir(result_state, data, config_dir)

    if args.debug_report_file:
        debug_report = data.get("debug_report")
        if debug_report:
            Path(args.debug_report_file).write_text(debug_report)
    if args.state_file:
        Path(args.state_file).write_text(json.dumps(data, indent=2, sort_keys=True))
    _print_run_state(data, args.format)
    return 0 if getattr(result_state, "status", None) == "completed" else 1


def _handle_doctor(args: argparse.Namespace) -> int:
    report = run_doctor(
        config_dir=args.config_dir,
        target_repo=args.target_repo,
        skill_overrides=args.override_skill,
        default_worker_overrides=args.override_default_worker,
        default_model_overrides=args.override_default_model,
        default_timeout_overrides=args.override_default_timeout,
        worker_overrides=args.override_worker,
        model_overrides=args.override_model,
        timeout_overrides=args.override_timeout,
        stage_extra_overrides=args.override_stage_extra,
        human_feedback_overrides=args.override_human_feedback,
        human_feedback_action_overrides=args.override_human_feedback_action,
        deliver_overrides=args.override_deliver,
        verify_overrides=args.override_verify,
        worker_binary_overrides=args.override_worker_binary,
    )
    _print_report(report, args.format)
    return 1 if report.status == "fail" else 0


def _handle_preflight(args: argparse.Namespace) -> int:
    report = run_preflight(args.target_repo, config_dir=args.config_dir)
    _print_report(report, args.format)
    return 1 if report.status == "fail" else 0


def _handle_enqueue(args: argparse.Namespace) -> int:
    from .job_queue import enqueue_job, new_job

    target_repo = normalize_path(args.target_repo)
    if not target_repo.is_dir():
        raise ValueError(f"--target-repo is not a directory: {target_repo}")
    config_dir = normalize_path(args.config_dir) if args.config_dir else None
    if config_dir is not None and not config_dir.is_dir():
        raise ValueError(f"--config-dir is not a directory: {config_dir}")

    job = new_job(
        args.request,
        str(target_repo),
        max_revisions=_resolve_max_revisions(args.max_revisions),
        config_dir=str(config_dir) if config_dir else None,
        overrides=_collect_override_kinds(args),
    )
    path = enqueue_job(normalize_path(args.queue_dir), job)
    if args.format == "json":
        print(json.dumps({"job_id": job.job_id, "path": str(path)}, indent=2))
    else:
        print(f"Enqueued job {job.job_id}")
        print(f"Job file: {path}")
    return 0


def _handle_serve(args: argparse.Namespace) -> int:
    from .job_queue import serve_queue

    if args.max_concurrent < 1:
        raise ValueError("--max-concurrent must be at least 1")
    if args.poll_interval <= 0:
        raise ValueError("--poll-interval must be positive")

    report = serve_queue(
        normalize_path(args.queue_dir),
        runs_dir=_resolve_runs_dir(args.runs_dir),
        max_concurrent=args.max_concurrent,
        poll_interval=args.poll_interval,
        once=args.once,
    )
    if args.format == "json":
        print(json.dumps(report.model_dump(), indent=2, sort_keys=True))
    else:
        print(
            f"Processed: {report.processed} "
            f"(done: {len(report.done)}, failed: {len(report.failed)}, "
            f"requeued: {len(report.requeued)})"
        )
        for job_id in report.failed:
            print(f"- failed: {job_id}")
    return 0 if not report.failed else 1


def _handle_dashboard(args: argparse.Namespace) -> int:
    from .dashboard import start_dashboard

    start_dashboard(
        host=args.host,
        port=args.port,
        queue_dir=args.queue_dir,
        runs_dir=args.runs_dir,
    )
    return 0


def _handle_runs(args: argparse.Namespace) -> int:
    from .run_store import summarize_runs

    runs_dir = _resolve_runs_dir(args.runs_dir)
    if runs_dir is None:
        raise ValueError("runs requires a runs directory (--runs-dir)")
    summaries = summarize_runs(runs_dir, limit=args.limit)
    if args.format == "json":
        print(json.dumps(summaries, indent=2, sort_keys=True))
        return 0

    if not summaries:
        print(f"No runs found in {runs_dir}")
        return 0
    for entry in summaries:
        revisions = (
            f"{entry['revisions']}/{entry['max_revisions']}"
            if entry.get("revisions") is not None
            else "-"
        )
        tasks = (
            f"{entry['tasks_done']}/{entry['tasks_total']}"
            if entry.get("tasks_total") is not None
            else "-"
        )
        line = (
            f"{entry['run_id']}  {entry['status']:<12} "
            f"rev {revisions:<5} tasks {tasks:<7}"
        )
        if entry.get("branch"):
            line += f" -> {entry['branch']}"
        print(line)
        if entry.get("request"):
            print(f"    {entry['request']}")
    return 0


def _handle_jobs(args: argparse.Namespace) -> int:
    from .job_queue import list_jobs

    queue_dir = normalize_path(args.queue_dir)
    snapshot = list_jobs(queue_dir)
    states = [args.state] if args.state else list(snapshot)
    if args.format == "json":
        payload = {
            state: [job.model_dump() for job in snapshot[state]] for state in states
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if not any(snapshot[state] for state in states):
        print(f"No jobs found in {queue_dir}")
        return 0
    for state in states:
        jobs = snapshot[state]
        print(f"{state} ({len(jobs)}):")
        for job in jobs:
            line = f"  {job.job_id}"
            if job.run_status:
                line += f"  {job.run_status}"
            if job.exit_code is not None:
                line += f"  exit {job.exit_code}"
            print(line)
            request = _compact_text(job.request)
            if request:
                print(f"    {request}")
            error = _compact_text(job.error)
            if error:
                print(f"    error: {error}")
    return 0


def _collect_override_kinds(args: argparse.Namespace) -> dict[str, list[str]]:
    """Map parsed --override-* flags onto job_queue override kinds."""
    overrides: dict[str, list[str]] = {}
    for attr, values in vars(args).items():
        if not attr.startswith("override_") or not values:
            continue
        overrides[attr.removeprefix("override_").replace("_", "-")] = list(values)
    return overrides


def _print_report(report: diagnostics.DiagnosticReport, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        return

    for check in report.checks:
        print(f"{check.status.upper():<4} {check.name}: {check.message}")
    _print_diagnostic_report_summary(report)
    print(f"Status: {report.status}")


def _state_to_dict(state) -> dict:
    if hasattr(state, "model_dump"):
        return state.model_dump()
    return dict(getattr(state, "__dict__", {}))


def _load_state_file(path: str) -> FlowState:
    data = json.loads(Path(path).read_text())
    return FlowState.model_validate(data)


def _resolve_runs_dir(raw: str | None) -> Path | None:
    if raw is None or raw.strip().lower() in {"", "none"}:
        return None
    return normalize_path(raw)


def _resolve_run_config_dir(
    explicit_config_dir: Path | None,
    state: FlowState | None,
) -> Path:
    if explicit_config_dir is not None:
        return explicit_config_dir

    saved_config_dir = state.config_dir if state is not None else None
    if saved_config_dir:
        return normalize_path(saved_config_dir)

    return DEFAULT_CONFIG_DIR.resolve()


def _attach_config_dir(state: object, data: dict, config_dir: Path) -> None:
    config_text = str(config_dir)
    try:
        setattr(state, "config_dir", config_text)
    except Exception:
        pass
    data["config_dir"] = config_text


def _resolve_max_revisions(raw: int | None) -> int | None:
    if raw is None:
        return None
    if raw < 1:
        raise ValueError("--max-revisions must be at least 1")
    return raw


def _print_run_state(data: dict, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(data, indent=2, sort_keys=True))
        return

    print(f"Status: {data.get('status', 'unknown')}")
    print(f"Revisions: {data.get('revisions', 0)}/{data.get('max_revisions', 0)}")
    tasks = data.get("tasks") or []
    if isinstance(tasks, list) and tasks:
        done = sum(1 for task in tasks if task.get("status") == "done")
        print(f"Tasks: {done}/{len(tasks)} done")
    changed_files = data.get("changed_files") or []
    print(f"Changed files tracked: {len(changed_files)}")
    run_id = data.get("run_id")
    if run_id:
        print(f"Run: {run_id} ({data.get('run_dir', '?')})")
    delivery = data.get("delivery_report")
    if isinstance(delivery, dict):
        summary = delivery.get("status", "unknown")
        if delivery.get("branch"):
            summary += f" on {delivery['branch']}"
        if delivery.get("commit_sha"):
            summary += f" @ {str(delivery['commit_sha'])[:12]}"
        print(f"Delivery: {summary}")
    _print_aborted_checkpoint_summary(
        data.get("aborted_checkpoint"),
        latest_work_summary=data.get("latest_work_summary"),
    )
    _print_pending_revision_replan_summary(
        data.get("pending_revision_replan"),
        data.get("pending_revision_replan_reason"),
    )
    _print_compact_list_block("Issues", data.get("issues"))
    _print_compact_list_block("Errors", data.get("errors"))

    final_artifact = data.get("final_artifact")
    if final_artifact:
        print("\nFinal Artifact:")
        print(final_artifact)

    debug_report = data.get("debug_report")
    if debug_report:
        print("\nDebug Report: available via --format json or --debug-report-file")


def _print_compact_list_block(label: str, value: object, *, limit: int = 3) -> None:
    if not isinstance(value, list):
        return

    items = [str(item).strip() for item in value if str(item).strip()]
    if not items:
        return

    print(f"{label}:")
    for item in items[:limit]:
        print(f"- {item}")
    if len(items) > limit:
        print(f"- ... ({len(items) - limit} more)")


def _print_diagnostic_report_summary(report: diagnostics.DiagnosticReport) -> None:
    context_lines: list[str] = []
    if report.config_dir:
        context_lines.append(f"Config dir: {report.config_dir}")
    if report.target_repo:
        context_lines.append(f"Target repo: {report.target_repo}")
    if context_lines:
        print("")
        for line in context_lines:
            print(line)

    if report.tooling:
        present = [name for name, exists in report.tooling.items() if exists]
        missing = [name for name, exists in report.tooling.items() if not exists]
        print("")
        print(f"Tooling: {len(present)}/{len(report.tooling)} present")
        if missing:
            print(f"Missing tooling: {', '.join(missing)}")

    if report.git:
        print("")
        print(_git_summary_line(report.git))
        porcelain = report.git.get("porcelain")
        if isinstance(porcelain, list):
            entries = [str(line).strip() for line in porcelain if str(line).strip()]
            if entries:
                print("Git porcelain:")
                for line in entries[:5]:
                    print(f"- {line}")
                if len(entries) > 5:
                    print(f"- ... ({len(entries) - 5} more)")

    resolved_runtime = report.resolved_runtime
    if not isinstance(resolved_runtime, dict) or not resolved_runtime:
        return

    print("")
    print("Resolved Runtime:")
    stages = resolved_runtime.get("stages")
    if isinstance(stages, list):
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            model = stage.get("model") or "(default)"
            print(
                f"- {stage.get('stage')}: skill={stage.get('skill')} | "
                f"worker={stage.get('worker')} | model={model} | "
                f"timeout={stage.get('timeout')}s | "
                f"mutates={'yes' if stage.get('can_mutate') else 'no'}"
            )
            runtime_knobs = stage.get("runtime_knobs")
            if isinstance(runtime_knobs, dict) and runtime_knobs:
                print(f"  Runtime knobs: {json.dumps(runtime_knobs, sort_keys=True)}")
            enforced = stage.get("enforced_declarations")
            if isinstance(enforced, dict) and enforced:
                print(
                    f"  Enforced declarations: {json.dumps(enforced, sort_keys=True)}"
                )
            notes = stage.get("notes")
            if isinstance(notes, list):
                note_items = [str(note).strip() for note in notes if str(note).strip()]
                if note_items:
                    print(f"  Notes: {', '.join(note_items)}")

    human_feedback = resolved_runtime.get("human_feedback")
    if isinstance(human_feedback, dict) and human_feedback:
        print(f"- human_feedback: {json.dumps(human_feedback, sort_keys=True)}")


def _git_summary_line(git: dict[str, object]) -> str:
    if git.get("git_available") is False:
        return "Git: unavailable"
    if git.get("is_git_repo") is not True:
        return "Git: non-git directory"
    if git.get("status_known") is False:
        return "Git: status unavailable"

    branch = git.get("branch")
    branch_text = (
        str(branch).strip() if isinstance(branch, str) and branch.strip() else None
    )
    if git.get("detached_head") is True:
        branch_text = "(detached)"

    return (
        "Git: "
        f"{'dirty' if git.get('is_dirty') else 'clean'} | "
        f"branch={branch_text or '(unknown)'} | "
        f"staged={'yes' if git.get('staged') else 'no'} | "
        f"unstaged={'yes' if git.get('unstaged') else 'no'} | "
        f"untracked={'yes' if git.get('untracked') else 'no'} | "
        f"conflicts={'yes' if git.get('has_conflicts') else 'no'}"
    )


def _print_aborted_checkpoint_summary(
    checkpoint: object, *, latest_work_summary: object | None = None
) -> None:
    if not isinstance(checkpoint, dict):
        return

    stage = checkpoint.get("stage")
    gate = checkpoint.get("gate")
    if isinstance(stage, str):
        print(
            "Aborted checkpoint: "
            + human_feedback_target_label(
                stage,
                gate if isinstance(gate, str) else None,
                include_default_gate=True,
            )
        )

    message = _compact_text(checkpoint.get("message"))
    if message:
        print(f"Abort message: {message}")

    if _compact_text(checkpoint.get("before_review_instructions")):
        print("Saved before_review instructions: yes")
    stage_input = _compact_text(checkpoint.get("stage_input"))
    if stage_input:
        print("Resume input captured: yes")
    latest_work = _compact_text(latest_work_summary)
    if latest_work and latest_work != stage_input:
        print("Latest review input captured: yes")


def _print_pending_revision_replan_summary(pending: object, reason: object) -> None:
    if pending is not True:
        return

    summary = _compact_text(reason)
    if summary:
        print(f"Pending revision replan: {summary}")
    else:
        print("Pending revision replan: yes")


def _compact_text(value: object, *, max_chars: int = 120) -> str | None:
    if not isinstance(value, str):
        return None

    compact = " ".join(value.split())
    if not compact:
        return None
    if len(compact) > max_chars:
        return f"{compact[: max_chars - 3]}..."
    return compact


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
    _add_runtime_override_args(parser)
    return parser.parse_args(args)


def _parse_preflight_args(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m crewai_headless_flow preflight")
    parser.add_argument("--target-repo", required=True)
    parser.add_argument("--config-dir")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args(args)


def _parse_enqueue_args(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m crewai_headless_flow enqueue")
    parser.add_argument("--request", required=True)
    parser.add_argument("--target-repo", required=True)
    parser.add_argument("--queue-dir", default="./queue")
    parser.add_argument("--max-revisions", type=int)
    parser.add_argument("--config-dir")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    _add_runtime_override_args(parser)
    return parser.parse_args(args)


def _parse_serve_args(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m crewai_headless_flow serve")
    parser.add_argument("--queue-dir", default="./queue")
    parser.add_argument(
        "--runs-dir",
        default="./runs",
        help="Base directory for the spawned runs' artifact dirs.",
    )
    parser.add_argument("--max-concurrent", type=int, default=1)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument(
        "--once",
        action="store_true",
        help="Drain the queue and exit instead of polling forever.",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args(args)


def _parse_runs_args(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m crewai_headless_flow runs")
    parser.add_argument("--runs-dir", default="./runs")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args(args)


def _parse_jobs_args(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m crewai_headless_flow jobs")
    parser.add_argument("--queue-dir", default="./queue")
    parser.add_argument(
        "--state",
        choices=("pending", "running", "done", "failed"),
        help="Only list jobs in this queue state.",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args(args)


def _parse_dashboard_args(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m crewai_headless_flow dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--queue-dir", default="./queue")
    parser.add_argument("--runs-dir", default="./runs")
    return parser.parse_args(args)


def _add_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--request")
    parser.add_argument("--target-repo")
    parser.add_argument("--max-revisions", type=int)
    parser.add_argument("--config-dir")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--debug-report-file")
    parser.add_argument("--state-file")
    parser.add_argument("--resume-state-file")
    parser.add_argument(
        "--runs-dir",
        default="./runs",
        help=(
            "Base directory for per-run artifact directories "
            "(runs/<run_id>/state.json + debug_report.md, checkpointed at "
            "every state mutation). Pass 'none' to disable."
        ),
    )
    _add_runtime_override_args(parser)


def _add_runtime_override_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--override-skill", action="append", default=[])
    parser.add_argument("--override-default-worker", action="append", default=[])
    parser.add_argument("--override-default-model", action="append", default=[])
    parser.add_argument("--override-default-timeout", action="append", default=[])
    parser.add_argument("--override-worker", action="append", default=[])
    parser.add_argument("--override-model", action="append", default=[])
    parser.add_argument("--override-timeout", action="append", default=[])
    parser.add_argument("--override-stage-extra", action="append", default=[])
    parser.add_argument("--override-human-feedback", action="append", default=[])
    parser.add_argument("--override-human-feedback-action", action="append", default=[])
    parser.add_argument("--override-deliver", action="append", default=[])
    parser.add_argument("--override-verify", action="append", default=[])
    parser.add_argument("--override-worker-binary", action="append", default=[])


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
    subparsers.add_parser("enqueue", help="Drop a request into the file-drop queue")
    subparsers.add_parser("serve", help="Drain the queue by spawning run subprocesses")
    subparsers.add_parser("runs", help="List run history from the runs directory")
    subparsers.add_parser("jobs", help="List queue jobs by state")
    subparsers.add_parser("dashboard", help="Start the local web dashboard")
    subparsers.add_parser("ui", help="Alias for dashboard")
    return parser


def _has_legacy_args(args: list[str]) -> bool:
    return "--request" in args or "--target-repo" in args


if __name__ == "__main__":
    raise SystemExit(main())
