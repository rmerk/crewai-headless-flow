"""
Per-run durable artifact store (autonomy Phase 1, Gap 3).

Every run gets an identity (`run_id`) and a home (`runs/<run_id>/`) where the
Flow checkpoints its state and debug report at every state mutation. Writes
are atomic (unique temp file in the same directory + os.replace), so a
checkpoint can never be observed torn — even with concurrent writers during
parallel task batches, the last complete write wins.

`summarize_runs` (autonomy Phase 3) is the read side: a run-history listing
over `runs/` built from each run's checkpointed `state.json`.

This module is deliberately dependency-light: it never imports flow/state and
takes pre-serialized strings, so it stays trivially testable and reusable.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

STATE_FILENAME = "state.json"
DEBUG_REPORT_FILENAME = "debug_report.md"
PENDING_APPROVAL_FILENAME = "pending_approval.json"
EVENTS_FILENAME = "events.jsonl"

_SLUG_MAX_LEN = 24


def slugify_request(request: str, max_len: int = _SLUG_MAX_LEN) -> str:
    """Reduce a free-text request to a filesystem-safe lowercase slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", request.lower()).strip("-")
    slug = slug[:max_len].rstrip("-")
    return slug or "run"


def generate_run_id(
    request: str,
    *,
    now: datetime | None = None,
    uuid_hex: str | None = None,
) -> str:
    """Build a sortable, human-scannable run id: <timestamp>-<slug>-<uuid8>."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    suffix = (uuid_hex or uuid.uuid4().hex)[:8]
    return f"{stamp}-{slugify_request(request)}-{suffix}"


class RunStore:
    """Owns one run directory and its durable artifacts."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)

    @classmethod
    def allocate(
        cls,
        base_dir: Path | str,
        request: str,
        *,
        now: datetime | None = None,
        uuid_hex: str | None = None,
    ) -> "RunStore":
        run_id = generate_run_id(request, now=now, uuid_hex=uuid_hex)
        run_dir = Path(base_dir) / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        return cls(run_dir)

    @classmethod
    def attach(cls, run_dir: Path | str) -> "RunStore":
        run_dir = Path(run_dir)
        if not run_dir.is_dir():
            raise FileNotFoundError(f"Run directory does not exist: {run_dir}")
        return cls(run_dir)

    @property
    def run_id(self) -> str:
        return self.run_dir.name

    @property
    def state_path(self) -> Path:
        return self.run_dir / STATE_FILENAME

    @property
    def debug_report_path(self) -> Path:
        return self.run_dir / DEBUG_REPORT_FILENAME

    def pending_approval_path(self) -> Path:
        return self.run_dir / PENDING_APPROVAL_FILENAME

    @property
    def events_path(self) -> Path:
        return self.run_dir / EVENTS_FILENAME

    def save_state(self, state_json: str) -> None:
        self._atomic_write(self.state_path, state_json)

    def save_debug_report(self, report: str) -> None:
        self._atomic_write(self.debug_report_path, report)

    def append_event(self, line: str) -> None:
        """Append one pre-serialized JSON line to events.jsonl.

        Unlike state/report snapshots this is append-only (a resumed run
        continues the same file), so it uses a plain append-mode write —
        one write() call per line, not the replace-based atomic path.
        """
        with self.events_path.open("a") as handle:
            handle.write(line + "\n")

    def _atomic_write(self, target: Path, content: str) -> None:
        fd, tmp_name = tempfile.mkstemp(
            dir=self.run_dir, prefix=f".{target.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(content)
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def summarize_runs(base_dir: Path | str, *, limit: int | None = None) -> list[dict]:
    """Run-history listing over ``base_dir`` (newest first).

    One summary dict per run directory, read from its checkpointed
    ``state.json``. Run ids start with a timestamp, so reverse-lexical
    order is reverse-chronological. A run dir with a missing or unreadable
    state file still gets a row (status ``"unknown"``) — an operator
    scanning history must see crashed-before-first-checkpoint runs too.
    """
    base_dir = Path(base_dir)
    if not base_dir.is_dir():
        return []

    summaries: list[dict] = []
    run_dirs = sorted(
        (entry for entry in base_dir.iterdir() if entry.is_dir()),
        key=lambda entry: entry.name,
        reverse=True,
    )
    if limit is not None:
        run_dirs = run_dirs[:limit]
    for run_dir in run_dirs:
        summaries.append(_summarize_run_dir(run_dir))
    return summaries


def _summarize_run_dir(run_dir: Path) -> dict:
    summary: dict[str, Any] = {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "status": "unknown",
        "request": None,
        "revisions": None,
        "max_revisions": None,
        "tasks_done": None,
        "tasks_total": None,
        "branch": None,
        "changed_files": None,
    }
    try:
        data = json.loads((run_dir / STATE_FILENAME).read_text())
    except (OSError, ValueError):
        return summary
    if not isinstance(data, dict):
        return summary

    summary["status"] = str(data.get("status") or "unknown")
    request = data.get("request")
    summary["request"] = " ".join(str(request).split())[:80] if request else None
    summary["revisions"] = data.get("revisions")
    summary["max_revisions"] = data.get("max_revisions")
    tasks = data.get("tasks")
    if isinstance(tasks, list):
        summary["tasks_total"] = len(tasks)
        summary["tasks_done"] = sum(
            1
            for task in tasks
            if isinstance(task, dict) and task.get("status") == "done"
        )
    delivery = data.get("delivery_report")
    if isinstance(delivery, dict):
        summary["branch"] = delivery.get("branch")
    changed = data.get("changed_files")
    if isinstance(changed, list):
        summary["changed_files"] = len(changed)
    return summary
