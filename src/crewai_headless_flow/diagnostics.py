"""Read-only diagnostics for CLI doctor and target-repo preflight."""

from __future__ import annotations

import subprocess
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from .config import load_config


Status = Literal["pass", "warn", "fail"]
REQUIRED_STAGES = ("plan", "do_work", "review", "finalize")
SUPPORTED_WORKERS = {"codex", "grok", "claude"}
WORKER_BINARIES = {"codex": "codex", "grok": "grok", "claude": "claude"}
WORKER_HELP_COMMANDS = {
    "codex": ("codex", "exec", "--help"),
    "grok": ("grok", "--help"),
    "claude": ("claude", "--help"),
}
WORKER_REQUIRED_FLAGS = {
    "codex": ("--sandbox", "--output-schema"),
    "grok": ("--always-approve", "--output-format"),
    "claude": ("--permission-mode", "--json-schema"),
}
TOOLING_FILES = (
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "pytest.ini",
    "README.md",
)
OUTPUT_LIMIT = 2000


@dataclass(frozen=True)
class ProbeResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class DiagnosticCheck:
    name: str
    status: Status
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "details": _to_primitive(self.details),
        }


@dataclass
class DiagnosticReport:
    status: Status = "pass"
    checks: list[DiagnosticCheck] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    config_dir: str | None = None
    target_repo: str | None = None
    git: dict[str, Any] = field(default_factory=dict)
    tooling: dict[str, Any] = field(default_factory=dict)

    def add_check(
        self,
        name: str,
        status: Status,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.checks.append(DiagnosticCheck(name, status, message, details or {}))
        if status == "fail":
            self.failures.append(message)
            self.status = "fail"
        elif status == "warn":
            self.warnings.append(message)
            if self.status == "pass":
                self.status = "warn"

    def merge(self, other: "DiagnosticReport") -> None:
        self.checks.extend(other.checks)
        self.warnings.extend(other.warnings)
        self.failures.extend(other.failures)
        if other.target_repo is not None:
            self.target_repo = other.target_repo
        if other.git:
            self.git = other.git
        if other.tooling:
            self.tooling = other.tooling
        self.status = _aggregate_status(self.status, other.status)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "status": self.status,
            "checks": [check.to_dict() for check in self.checks],
            "warnings": list(self.warnings),
            "failures": list(self.failures),
        }
        if self.config_dir is not None:
            data["config_dir"] = self.config_dir
        if self.target_repo is not None:
            data["target_repo"] = self.target_repo
        if self.git:
            data["git"] = _to_primitive(self.git)
        if self.tooling:
            data["tooling"] = _to_primitive(self.tooling)
        return data


def normalize_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def run_doctor(
    *,
    config_dir: str | Path | None = None,
    target_repo: str | Path | None = None,
    skills_root: str | Path | None = None,
) -> DiagnosticReport:
    config_path = normalize_path(config_dir or _default_config_dir())
    report = DiagnosticReport(config_dir=str(config_path))

    skills_path = config_path / "skills.yaml"
    worker_path = config_path / "worker.yaml"
    skills_raw = _read_yaml_mapping(skills_path, report, "skills.yaml")
    worker_raw = _read_yaml_mapping(worker_path, report, "worker.yaml")

    required_workers: set[str] = set()
    if skills_raw is not None and worker_raw is not None:
        required_workers = _validate_config_files(
            report=report,
            config_path=config_path,
            skills_raw=skills_raw,
            worker_raw=worker_raw,
            skills_root=normalize_path(skills_root)
            if skills_root is not None
            else _default_skills_root(config_path),
        )

    for worker in sorted(required_workers):
        _check_worker_cli(report, worker)

    _check_ollama(report)

    if target_repo is not None:
        report.merge(run_preflight(target_repo))

    return report


def run_preflight(
    target_repo: str | Path, config_dir: str | Path | None = None
) -> DiagnosticReport:
    target = normalize_path(target_repo)
    report = DiagnosticReport(
        target_repo=str(target),
        config_dir=str(normalize_path(config_dir)) if config_dir is not None else None,
    )

    if not target.exists():
        report.add_check(
            "target.exists",
            "fail",
            f"Target repo does not exist: {target}",
        )
        return report
    if not target.is_dir():
        report.add_check(
            "target.directory",
            "fail",
            f"Target repo is not a directory: {target}",
        )
        return report

    report.add_check("target.directory", "pass", f"Target repo exists: {target}")
    report.tooling = {name: (target / name).exists() for name in TOOLING_FILES}
    report.add_check(
        "target.tooling",
        "pass",
        "Target tooling files inspected",
        {"tooling": report.tooling},
    )

    _check_git(report, target)
    return report


def _validate_config_files(
    *,
    report: DiagnosticReport,
    config_path: Path,
    skills_raw: dict[str, Any],
    worker_raw: dict[str, Any],
    skills_root: Path,
) -> set[str]:
    stages = skills_raw.get("stages")
    if not isinstance(stages, dict):
        report.add_check(
            "config.skills.stages",
            "fail",
            "skills.yaml must contain a mapping at stages",
        )
        return set()

    missing = [stage for stage in REQUIRED_STAGES if stage not in stages]
    if missing:
        report.add_check(
            "config.skills.required_stages",
            "fail",
            f"skills.yaml missing required stages: {', '.join(missing)}",
        )
    else:
        report.add_check(
            "config.skills.required_stages",
            "pass",
            "skills.yaml contains all required stages",
        )

    for stage, skill in stages.items():
        skill_path = skills_root / str(skill) / "SKILL.md"
        if not skill_path.exists():
            report.add_check(
                f"config.skills.{stage}",
                "fail",
                f"Skill not found for stage {stage}: {skill}",
                {"expected_path": str(skill_path)},
            )

    workers = worker_raw.get("stages", {}) or {}
    defaults = worker_raw.get("defaults", {}) or {}
    if not isinstance(workers, dict):
        report.add_check(
            "config.workers.stages", "fail", "worker.yaml stages must be a mapping"
        )
        workers = {}
    if not isinstance(defaults, dict):
        report.add_check(
            "config.workers.defaults", "fail", "worker.yaml defaults must be a mapping"
        )
        defaults = {}

    required_workers: set[str] = set()
    default_worker = defaults.get("worker")
    if default_worker is not None and default_worker not in SUPPORTED_WORKERS:
        report.add_check(
            "config.workers.default",
            "fail",
            f"Unsupported worker in defaults: {default_worker}",
        )
    for stage in REQUIRED_STAGES:
        stage_cfg = workers.get(stage, {}) or {}
        if not isinstance(stage_cfg, dict):
            report.add_check(
                f"config.workers.{stage}",
                "fail",
                f"worker.yaml stage {stage} must be a mapping",
            )
            continue
        worker = stage_cfg.get("worker", default_worker)
        if worker is None:
            report.add_check(
                f"config.workers.{stage}",
                "fail",
                f"No worker configured for stage {stage} and no default worker is set",
            )
            continue
        if worker not in SUPPORTED_WORKERS:
            report.add_check(
                f"config.workers.{stage}",
                "fail",
                f"Unsupported worker for stage {stage}: {worker}",
            )
            continue
        required_workers.add(str(worker))

    try:
        cfg = load_config(config_path)
    except Exception as exc:
        report.add_check("config.load", "fail", f"load_config failed: {exc}")
    else:
        report.add_check(
            "config.load",
            "pass",
            "Configuration loads through load_config",
            {"stages": cfg.stages},
        )
        crew_cfg = (
            cfg.get_stage("review").extra.get("crew", {})
            if "review" in cfg.stages
            else {}
        )
        if isinstance(crew_cfg, dict) and crew_cfg.get("enabled", False):
            report.add_check(
                "config.review_crew",
                "warn",
                "Review Crew is enabled; its LLM readiness depends on configured provider",
                {"llm": crew_cfg.get("llm", {})},
            )

    return required_workers


def _read_yaml_mapping(
    path: Path, report: DiagnosticReport, label: str
) -> dict[str, Any] | None:
    if not path.exists():
        report.add_check(f"config.{label}", "fail", f"{label} not found: {path}")
        return None
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except Exception as exc:
        report.add_check(
            f"config.{label}", "fail", f"{label} could not be parsed: {exc}"
        )
        return None
    if not isinstance(raw, dict):
        report.add_check(f"config.{label}", "fail", f"{label} must contain a mapping")
        return None
    report.add_check(f"config.{label}", "pass", f"{label} parsed")
    return raw


def _check_worker_cli(report: DiagnosticReport, worker: str) -> None:
    binary = WORKER_BINARIES[worker]
    if shutil.which(binary) is None:
        report.add_check(f"cli.{worker}", "fail", f"CLI not found: {binary}")
        return

    version = _run_probe((binary, "--version"))
    help_result = _run_probe(WORKER_HELP_COMMANDS[worker])
    help_text = _bounded_output(help_result.stdout + "\n" + help_result.stderr)
    if version.returncode != 0 and help_result.returncode != 0:
        report.add_check(
            f"cli.{worker}",
            "fail",
            f"{binary} did not respond to --version or --help",
            {"version": version.returncode, "help": help_result.returncode},
        )
        return

    missing_flags = [
        flag for flag in WORKER_REQUIRED_FLAGS[worker] if flag not in help_text
    ]
    if missing_flags:
        report.add_check(
            f"cli.{worker}.flags",
            "fail",
            f"{binary} help is missing required flags: {', '.join(missing_flags)}",
            {"output": help_text},
        )
        return

    report.add_check(
        f"cli.{worker}",
        "pass",
        f"{binary} CLI detected",
        {"version_returncode": version.returncode},
    )


def _check_ollama(report: DiagnosticReport) -> None:
    if shutil.which("ollama") is None:
        report.add_check("cli.ollama", "fail", "CLI not found: ollama")
        return

    result = _run_probe(("ollama", "list"))
    if result.returncode != 0:
        report.add_check(
            "cli.ollama",
            "fail",
            "ollama list failed",
            {"stderr": _bounded_output(result.stderr)},
        )
        return
    report.add_check(
        "cli.ollama",
        "pass",
        "ollama list succeeded",
        {"output": _bounded_output(result.stdout)},
    )


def _check_git(report: DiagnosticReport, target: Path) -> None:
    if shutil.which("git") is None:
        report.git = {"is_git_repo": False, "git_available": False}
        report.add_check("git.available", "warn", "git CLI not found")
        return

    inside = _run_probe(
        ("git", "-C", str(target), "rev-parse", "--is-inside-work-tree")
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        report.git = {"is_git_repo": False, "git_available": True}
        report.add_check("git.repo", "warn", "Target is a non-git directory")
        return

    branch = _run_probe(("git", "-C", str(target), "rev-parse", "--abbrev-ref", "HEAD"))
    status = _run_probe(("git", "-C", str(target), "status", "--porcelain"))
    if status.returncode != 0:
        report.git = {
            "is_git_repo": True,
            "git_available": True,
            "status_known": False,
        }
        report.add_check(
            "git.status",
            "warn",
            "git status failed",
            {"stderr": _bounded_output(status.stderr)},
        )
        return

    lines = [line for line in status.stdout.splitlines() if line.strip()]
    has_conflicts = any(_is_conflict_line(line) for line in lines)
    detached = branch.stdout.strip() == "HEAD"
    report.git = {
        "is_git_repo": True,
        "git_available": True,
        "status_known": True,
        "is_dirty": bool(lines),
        "has_conflicts": has_conflicts,
        "detached_head": detached,
        "staged": any(line[:1] not in {" ", "?"} for line in lines),
        "unstaged": any(len(line) > 1 and line[1] not in {" ", "?"} for line in lines),
        "untracked": any(line.startswith("??") for line in lines),
        "porcelain": lines,
    }
    if has_conflicts:
        report.add_check("git.conflicts", "fail", "Target repo has merge conflicts")
    elif lines:
        report.add_check("git.status", "warn", "Target repo has uncommitted changes")
    elif detached:
        report.add_check("git.branch", "warn", "Target repo is in detached HEAD state")
    else:
        report.add_check("git.status", "pass", "Target git repo is clean")


def _run_probe(cmd: tuple[str, ...], timeout: int = 3) -> ProbeResult:
    try:
        proc = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(returncode=124, stdout="", stderr="timed out")
    except Exception as exc:
        return ProbeResult(returncode=1, stdout="", stderr=str(exc))
    return ProbeResult(
        returncode=proc.returncode,
        stdout=_bounded_output(proc.stdout or ""),
        stderr=_bounded_output(proc.stderr or ""),
    )


def _default_config_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "config"


def _default_skills_root(config_path: Path) -> Path:
    repo_root = config_path.parent
    candidate = repo_root / "vendor" / "agent-skills" / "skills"
    if candidate.exists():
        return candidate
    return Path(__file__).resolve().parents[2] / "vendor" / "agent-skills" / "skills"


def _aggregate_status(left: Status, right: Status) -> Status:
    order = {"pass": 0, "warn": 1, "fail": 2}
    return left if order[left] >= order[right] else right


def _is_conflict_line(line: str) -> bool:
    return line[:2] in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


def _bounded_output(output: str) -> str:
    return output[:OUTPUT_LIMIT]


def _to_primitive(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _to_primitive(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_primitive(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
