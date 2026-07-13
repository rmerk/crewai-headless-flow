from __future__ import annotations

import json
import subprocess
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

import crewai_headless_flow.dashboard as dashboard
from crewai_headless_flow.job_queue import PENDING_DIR, LOGS_DIR, QueueJob

pytestmark = pytest.mark.offline


@pytest.fixture
def client(tmp_path):
    """Fixture to provide a TestClient and isolate the queue directory to a temp path."""
    old_queue = dashboard.QUEUE_DIR
    dashboard.QUEUE_DIR = tmp_path / "queue"
    dashboard.ensure_queue_dirs(dashboard.QUEUE_DIR)
    
    # Create client
    with TestClient(dashboard.app) as test_client:
        yield test_client
        
    # Restore
    dashboard.QUEUE_DIR = old_queue


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


def test_create_job_enqueues_correctly(client):
    payload = {
        "request": "Add a comment to the jenkinsfile",
        "target_repo": "/tmp/test-repo",
        "max_revisions": 2,
        "config_dir": "examples/configs/claude-do-work",
        "overrides": {"default-worker": ["claude"]}
    }
    
    response = client.post("/api/jobs", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["request"] == payload["request"]
    assert data["target_repo"] == payload["target_repo"]
    assert data["max_revisions"] == 2
    assert data["config_dir"] == payload["config_dir"]
    assert data["overrides"] == payload["overrides"]
    
    # Check that file exists on disk in pending/
    job_id = data["job_id"]
    job_file = dashboard.QUEUE_DIR / PENDING_DIR / f"{job_id}.json"
    assert job_file.exists()


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
    mock_item_portal.resolve.return_value = "/Users/rchoi/Developer/asure/asure.ptm.portal"
    
    mock_item_webapi = mocker.Mock(spec=Path)
    mock_item_webapi.is_dir.return_value = True
    mock_item_webapi.name = "asure.ptm.webapi"
    mock_item_webapi.resolve.return_value = "/Users/rchoi/Developer/asure/asure.ptm.webapi"
    
    mocker.patch("crewai_headless_flow.dashboard.Path.iterdir", return_value=[mock_item_portal, mock_item_webapi])
    
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
    
    mocker.patch("crewai_headless_flow.dashboard.Path.iterdir", return_value=[mock_pack])
    
    response = client.get("/api/config-packs")
    assert response.status_code == 200
    packs = response.json()
    assert "jira-workflow" in packs
