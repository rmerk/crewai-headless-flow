from __future__ import annotations

import json
import subprocess
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

import crewai_headless_flow.dashboard as dashboard
from crewai_headless_flow.job_queue import PENDING_DIR, LOGS_DIR

pytestmark = pytest.mark.offline


@pytest.fixture
def client(tmp_path):
    """Fixture to provide a TestClient and isolate the queue directory to a temp path."""
    old_queue = dashboard.QUEUE_DIR
    old_runs = dashboard.RUNS_DIR
    dashboard.QUEUE_DIR = tmp_path / "queue"
    dashboard.RUNS_DIR = tmp_path / "runs"
    dashboard.ensure_queue_dirs(dashboard.QUEUE_DIR)
    dashboard.RUNS_DIR.mkdir(parents=True, exist_ok=True)

    with TestClient(dashboard.app) as test_client:
        yield test_client

    dashboard.QUEUE_DIR = old_queue
    dashboard.RUNS_DIR = old_runs


def test_get_dashboard_html(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Antigravity Headless Coder" in response.text


def test_get_jobs_empty(client):
    response = client.get("/api/jobs")
    assert response.status_code == 200
    data = response.json()
    assert "pending" in data
    assert "running" in data
    assert "done" in data
    assert "failed" in data
    assert len(data["pending"]) == 0


def test_create_job_enqueues_correctly(client, tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    payload = {
        "request": "AS-5245",
        "target_repo": str(repo),
        "max_revisions": 3,
        "config_dir": str(config),
        "overrides": {"default-worker": ["claude"]},
    }

    response = client.post("/api/jobs", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["request"] == "AS-5245"
    assert data["target_repo"] == str(repo.resolve())
    assert data["max_revisions"] == 3
    assert data["config_dir"] == str(config)
    assert data["overrides"] == payload["overrides"]

    job_id = data["job_id"]
    job_file = dashboard.QUEUE_DIR / PENDING_DIR / f"{job_id}.json"
    assert job_file.exists()


def test_create_job_rejects_non_ticket_request(client, tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()
    response = client.post(
        "/api/jobs",
        json={"request": "add a feature", "target_repo": str(repo)},
    )
    assert response.status_code == 400
    assert "AS-####" in response.json()["detail"]


def test_get_job_logs(client):
    job_id = "test-job-123"

    # 1. Non-existent log file
    response = client.get(f"/api/jobs/{job_id}/logs")
    assert response.status_code == 200
    assert response.json() == {"log": ""}

    # 2. Existing log file
    log_path = dashboard.QUEUE_DIR / LOGS_DIR / f"{job_id}.log"
    log_path.write_text("Hello, world of logs!", encoding="utf-8")

    response = client.get(f"/api/jobs/{job_id}/logs")
    assert response.status_code == 200
    assert response.json() == {"log": "Hello, world of logs!"}


def test_cancel_job_not_running(client):
    response = client.post("/api/jobs/non-existent-job-id/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "not_running"


def test_cancel_job_active(client, mocker):
    job_id = "running-job-abc"

    # Mock subprocess.Popen
    mock_proc = mocker.Mock(spec=subprocess.Popen)
    mock_proc.poll.return_value = None  # means running

    # Inject active process
    with dashboard.ACTIVE_PROCESSES_LOCK:
        dashboard.ACTIVE_PROCESSES[job_id] = mock_proc

    response = client.post(f"/api/jobs/{job_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"

    mock_proc.terminate.assert_called_once()
    mock_proc.wait.assert_called_once()


def test_get_target_repos(client, mocker):
    mocker.patch("crewai_headless_flow.dashboard.Path.exists", return_value=True)
    mocker.patch("crewai_headless_flow.dashboard.Path.is_dir", return_value=True)

    mock_item_portal = mocker.Mock(spec=Path)
    mock_item_portal.is_dir.return_value = True
    mock_item_portal.name = "asure.ptm.portal"
    mock_item_portal.resolve.return_value = (
        "/Users/rchoi/Developer/asure/asure.ptm.portal"
    )

    mock_item_webapi = mocker.Mock(spec=Path)
    mock_item_webapi.is_dir.return_value = True
    mock_item_webapi.name = "asure.ptm.webapi"
    mock_item_webapi.resolve.return_value = (
        "/Users/rchoi/Developer/asure/asure.ptm.webapi"
    )

    mocker.patch(
        "crewai_headless_flow.dashboard.Path.iterdir",
        return_value=[mock_item_portal, mock_item_webapi],
    )

    response = client.get("/api/target-repos")
    assert response.status_code == 200
    repos = response.json()
    assert len(repos) == 2

    assert repos[0]["name"] == "asure.ptm.portal"
    assert repos[0]["role"] == "editable"
    assert repos[1]["name"] == "asure.ptm.webapi"
    assert repos[1]["role"] == "reference-only"


def test_get_config_packs(client, mocker):
    mocker.patch("crewai_headless_flow.dashboard.Path.exists", return_value=True)
    mocker.patch("crewai_headless_flow.dashboard.Path.is_dir", return_value=True)

    mock_pack = mocker.Mock(spec=Path)
    mock_pack.is_dir.return_value = True
    mock_pack.name = "jira-workflow"

    mocker.patch(
        "crewai_headless_flow.dashboard.Path.iterdir", return_value=[mock_pack]
    )

    response = client.get("/api/config-packs")
    assert response.status_code == 200
    packs = response.json()
    assert "jira-workflow" in packs


def test_pending_approval_list_and_continue_abort(client, tmp_path, mocker):
    run_id = "20260713-test-as-5245-abcdef12"
    run_dir = dashboard.RUNS_DIR / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "status": "aborted_by_human",
                "request": "AS-5245",
                "revisions": 1,
                "max_revisions": 3,
                "last_stage": "do_work",
                "errors": ["task t1 failed: boom"],
                "human_feedback_log": [
                    {
                        "gate": "before_do_work",
                        "trigger_reason": {
                            "kind": "repeated_task_failure",
                            "detail": {"task_id": "t1"},
                        },
                    }
                ],
                "config_dir": str(tmp_path / "config"),
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "pending_approval.json").write_text(
        json.dumps(
            {
                "gate": "before_do_work",
                "prompt": "Task keeps failing",
                "revisions": 1,
                "stage": "do_work",
                "target_repo": "/tmp/portal",
            }
        ),
        encoding="utf-8",
    )

    listed = client.get("/api/runs?pending_approval=true")
    assert listed.status_code == 200
    runs = listed.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["run_id"] == run_id
    assert runs[0]["request"] == "AS-5245"
    assert runs[0]["actions"] == ["continue", "abort"]

    brief = client.get(f"/api/runs/{run_id}/approval")
    assert brief.status_code == 200
    assert brief.json()["gate"] == "before_do_work"
    assert "boom" in brief.json()["error_tail"]

    answered = client.post(f"/api/runs/{run_id}/approval", json={"answer": "continue"})
    assert answered.status_code == 200
    assert answered.json()["answer"] == "y"
    pending = json.loads((run_dir / "pending_approval.json").read_text())
    assert pending["answer"] == "y"

    launched: dict = {}

    def fake_launch(argv, log_path):
        launched["argv"] = list(argv)
        launched["log_path"] = Path(log_path)
        proc = mocker.Mock(spec=subprocess.Popen)
        proc.pid = 4242
        proc.poll.return_value = None
        return proc

    mocker.patch(
        "crewai_headless_flow.dashboard.launch_run_subprocess",
        side_effect=fake_launch,
    )
    resumed = client.post(f"/api/runs/{run_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "resumed"
    assert "--resume-state-file" in launched["argv"]
    assert str(run_dir / "state.json") in launched["argv"]
