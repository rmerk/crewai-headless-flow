from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .job_queue import (
    LOGS_DIR,
    enqueue_job,
    ensure_queue_dirs,
    launch_run_subprocess,
    list_jobs,
    new_job,
    serve_queue,
)

logger = logging.getLogger(__name__)

# Global registry to track running subprocesses for cancellation
ACTIVE_PROCESSES: Dict[str, subprocess.Popen[bytes]] = {}
ACTIVE_PROCESSES_LOCK = threading.Lock()

# Global config variables set at server launch
QUEUE_DIR: Path = Path("queue")
RUNS_DIR: Optional[Path] = None


def prune_active_processes() -> None:
    """Remove terminated processes from the ACTIVE_PROCESSES registry."""
    with ACTIVE_PROCESSES_LOCK:
        for job_id in list(ACTIVE_PROCESSES.keys()):
            proc = ACTIVE_PROCESSES[job_id]
            if proc.poll() is not None:
                ACTIVE_PROCESSES.pop(job_id, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize queue directories and kick off the background serve worker thread."""
    ensure_queue_dirs(QUEUE_DIR)
    t = threading.Thread(
        target=run_serve_loop,
        args=(QUEUE_DIR, RUNS_DIR),
        daemon=True,
        name="DashboardQueueWorker",
    )
    t.start()
    logger.info("[Dashboard] Background queue worker thread launched.")
    yield
    # Clean up remaining active processes on shutdown
    with ACTIVE_PROCESSES_LOCK:
        for job_id, proc in list(ACTIVE_PROCESSES.items()):
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
            ACTIVE_PROCESSES.pop(job_id, None)


app = FastAPI(title="CrewAI Headless Flow Dashboard", lifespan=lifespan)


class EnqueueRequest(BaseModel):
    request: str
    target_repo: str
    config_dir: Optional[str] = None
    max_revisions: Optional[int] = None
    overrides: Dict[str, List[str]] = Field(default_factory=dict)


def dashboard_launcher(argv: List[str], log_path: Path) -> subprocess.Popen[bytes]:
    """Custom launcher that stores the running Popen process in a global registry for cancellation."""
    proc = launch_run_subprocess(argv, log_path)
    job_id = log_path.stem
    with ACTIVE_PROCESSES_LOCK:
        ACTIVE_PROCESSES[job_id] = proc
    logger.info(f"[Dashboard Launcher] Tracked job {job_id} in active registry.")
    return proc


def run_serve_loop(queue_dir: Path, runs_dir: Optional[Path]) -> None:
    """Run the queue serve loop indefinitely in a background thread."""
    try:
        logger.info(f"[Background Serve] Starting serve loop for queue: {queue_dir}")
        serve_queue(
            queue_dir=queue_dir,
            runs_dir=runs_dir,
            launcher=dashboard_launcher,
            poll_interval=1.0,
        )
    except Exception as exc:
        logger.error(f"[Background Serve] Loop crashed: {exc}", exc_info=True)


@app.get("/", response_class=HTMLResponse)
def get_dashboard() -> str:
    """Serve the single-page HTML interface."""
    html_path = Path(__file__).parent / "dashboard.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="dashboard.html template not found")
    return html_path.read_text(encoding="utf-8")


@app.get("/api/jobs")
def get_jobs() -> Dict[str, List[Dict[str, Any]]]:
    """Retrieve lists of jobs grouped by status (pending, running, done, failed)."""
    prune_active_processes()
    ensure_queue_dirs(QUEUE_DIR)
    snapshot = list_jobs(QUEUE_DIR)

    # Model serialization to plain dictionaries
    return {
        state: [job.model_dump() for job in jobs] for state, jobs in snapshot.items()
    }


@app.post("/api/jobs")
def create_job(payload: EnqueueRequest) -> Dict[str, Any]:
    """Enqueue a new run request into the job queue."""
    try:
        job = new_job(
            request=payload.request,
            target_repo=payload.target_repo,
            max_revisions=payload.max_revisions,
            config_dir=payload.config_dir,
            overrides=payload.overrides,
        )
        enqueue_job(QUEUE_DIR, job)
        return job.model_dump()
    except Exception as exc:
        logger.error(f"Failed to enqueue job: {exc}")
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/jobs/{job_id}/logs")
def get_job_logs(job_id: str) -> Dict[str, str]:
    """Fetch the log text file contents for a specific job."""
    if not re.match(r"^[a-zA-Z0-9_\-]+$", job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format.")
    log_path = QUEUE_DIR / LOGS_DIR / f"{job_id}.log"
    if not log_path.exists():
        return {"log": ""}
    try:
        return {"log": log_path.read_text(encoding="utf-8", errors="replace")}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read log file: {exc}")


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> Dict[str, str]:
    """Terminate the process of an active job."""
    if not re.match(r"^[a-zA-Z0-9_\-]+$", job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format.")

    with ACTIVE_PROCESSES_LOCK:
        proc = ACTIVE_PROCESSES.get(job_id)

    if proc is None:
        return {
            "status": "not_running",
            "message": f"Job {job_id} is not actively running in this process.",
        }

    try:
        # Check if process is still running
        if proc.poll() is None:
            logger.warning(f"Terminating subprocess for job {job_id}...")
            proc.terminate()
            proc.wait(timeout=2.0)
            with ACTIVE_PROCESSES_LOCK:
                ACTIVE_PROCESSES.pop(job_id, None)
            return {
                "status": "cancelled",
                "message": f"Job {job_id} successfully terminated.",
            }
        else:
            with ACTIVE_PROCESSES_LOCK:
                ACTIVE_PROCESSES.pop(job_id, None)
            return {
                "status": "finished",
                "message": f"Job {job_id} has already finished.",
            }
    except Exception as exc:
        # Fallback to force kill if terminate hangs or fails
        try:
            proc.kill()
            with ACTIVE_PROCESSES_LOCK:
                ACTIVE_PROCESSES.pop(job_id, None)
            return {"status": "killed", "message": f"Job {job_id} force-killed: {exc}"}
        except Exception as kill_exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to terminate process: {kill_exc}"
            )


@app.get("/api/target-repos")
def get_target_repos() -> List[Dict[str, str]]:
    """Scan and return potential target subdirectories in the user's workspace."""
    base_dir_env = os.environ.get("ASURE_BASE_DIR")
    base_dir = (
        Path(base_dir_env) if base_dir_env else Path("/Users/rchoi/Developer/asure")
    )
    repos = []

    if base_dir.exists() and base_dir.is_dir():
        for item in sorted(base_dir.iterdir(), key=lambda x: x.name):
            if item.is_dir() and not item.name.startswith("."):
                # Tag it as Portal or WebAPI based on folder name
                role = "reference-only" if "webapi" in item.name.lower() else "editable"
                repos.append(
                    {"name": item.name, "path": str(item.resolve()), "role": role}
                )
    return repos


@app.get("/api/config-packs")
def get_config_packs() -> List[str]:
    """Scan and list the workflow configuration packs under examples/configs/."""
    config_root = Path(__file__).resolve().parents[2] / "examples" / "configs"
    packs = []

    if config_root.exists() and config_root.is_dir():
        for item in sorted(config_root.iterdir(), key=lambda x: x.name):
            if item.is_dir() and not item.name.startswith("."):
                packs.append(item.name)
    return packs


def start_dashboard(
    host: str = "127.0.0.1",
    port: int = 8000,
    queue_dir: str = "queue",
    runs_dir: Optional[str] = None,
) -> None:
    """Launcher method called by the CLI handler to run Uvicorn."""
    global QUEUE_DIR, RUNS_DIR
    QUEUE_DIR = Path(queue_dir)
    RUNS_DIR = Path(runs_dir) if runs_dir else None

    logger.info(f"Launching dashboard at http://{host}:{port} ...")
    uvicorn.run(app, host=host, port=port, log_level="info")
