import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import queue as q
from app import status
from app.main import create_app


def _sample_payload(identifier="BAS-115", issue_id="issue-115"):
    return {
        "data": {
            "id": issue_id,
            "identifier": identifier,
            "title": "Add status CLI",
            "state": {"name": "AI Implementation"},
        }
    }


def _dashboard_payload(identifier="BAS-124", issue_id="issue-124", *, description="folder: D:/bench/example-repo"):
    return {
        "data": {
            "id": issue_id,
            "identifier": identifier,
            "title": "Extend status JSON",
            "description": description,
            "project": {"name": "Linear Executor"},
            "labels": [{"name": "cli:opencode"}, {"name": "auth:oauth"}],
        }
    }


def _set_job_state(db_path, job_id, *, state, started_at=None, completed_at=None, last_error=None):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, started_at = ?, completed_at = ?, last_error = ?
            WHERE id = ?
            """,
            (state, started_at, completed_at, last_error, job_id),
        )


def _set_job_times(db_path, job_id, *, created_at=None, started_at=None, completed_at=None):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET created_at=COALESCE(?, created_at),
                started_at=?,
                completed_at=?
            WHERE id=?
            """,
            (created_at, started_at, completed_at, job_id),
        )


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "jobs.db"
    q.init_db(path)
    return path


def test_read_queue_status_formats_jobs_and_summary(tmp_path):
    db_path = tmp_path / "jobs.db"
    q.init_db(db_path)
    running_id = q.enqueue(db_path, kind="start", payload=_sample_payload("BAS-115"), delivery_id="d-1")
    pending_id = q.enqueue(db_path, kind="proxy", payload=_sample_payload("BAS-116", "issue-116"), delivery_id="d-2")
    failed_id = q.enqueue(db_path, kind="complete", payload=_sample_payload("BAS-117", "issue-117"), delivery_id="d-3")

    _set_job_state(db_path, running_id, state="running", started_at="2026-06-19 11:58:30")
    _set_job_state(
        db_path,
        failed_id,
        state="failed",
        completed_at="2026-06-19 11:59:00",
        last_error="first line\nsecond line with extra detail",
    )

    data = status.read_queue_status(
        db_path,
        recent=3,
        now=datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC),
    )

    assert data["database"] == str(db_path)
    assert data["queue"] == {
        "pending": 1,
        "running": 1,
        "done": 0,
        "failed": 1,
        "cancelled": 0,
    }
    assert data["running"] == [
        {
            "id": running_id,
            "ticket_id": "issue-115",
            "issue": "BAS-115",
            "kind": "start",
            "status": "running",
            "retries": 0,
            "max_retries": 3,
            "created_at": data["running"][0]["created_at"],
            "started_at": "2026-06-19 11:58:30",
            "completed_at": None,
            "elapsed": "1m 30s",
            "last_error": None,
        }
    ]
    assert [job["id"] for job in data["pending"]] == [pending_id]
    assert data["recent_failures"][0]["id"] == failed_id
    assert data["recent_failures"][0]["last_error"] == "first line second line with extra detail"


def test_read_queue_status_filters_by_issue_and_running_only(tmp_path):
    db_path = tmp_path / "jobs.db"
    q.init_db(db_path)
    q.enqueue(db_path, kind="start", payload=_sample_payload("BAS-115"), delivery_id="d-1")
    running_id = q.enqueue(db_path, kind="start", payload=_sample_payload("BAS-116", "issue-116"), delivery_id="d-2")
    _set_job_state(db_path, running_id, state="running", started_at="2026-06-19 12:00:00")

    data = status.read_queue_status(db_path, issue="BAS-116", running_only=True)

    assert data["queue"]["pending"] == 0
    assert data["queue"]["running"] == 1
    assert data["running"][0]["id"] == running_id
    assert data["pending"] == []


def test_format_human_includes_tables_and_database_path(tmp_path):
    db_path = tmp_path / "jobs.db"
    q.init_db(db_path)
    running_id = q.enqueue(db_path, kind="review_watch", payload=_sample_payload(), delivery_id="d-1")
    _set_job_state(db_path, running_id, state="running", started_at="2026-06-19 11:59:55")
    data = status.build_status(
        db_path,
        health_url=None,
        include_processes=False,
        now=datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC),
    )

    output = status.format_human(data)

    assert f"Database: {db_path}" in output
    assert "Queue: pending=0, running=1, done=0, failed=0, cancelled=0" in output
    assert "Running jobs" in output
    assert "BAS-115" in output
    assert "review_watch" in output
    assert "5s" in output


def test_main_json_output_is_machine_readable(tmp_path, capsys):
    db_path = tmp_path / "jobs.db"
    q.init_db(db_path)
    q.enqueue(db_path, kind="start", payload=_sample_payload(), delivery_id="d-1")

    exit_code = status.main(["--db", str(db_path), "--json", "--no-health", "--no-processes"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["database"] == str(db_path)
    assert payload["queue"]["pending"] == 1
    assert payload["health"] is None
    assert payload["backend_processes"] == []


def test_default_health_url_matches_local_executor_port():
    assert status.DEFAULT_HEALTH_URL == "http://127.0.0.1:8123/health"


def test_missing_database_reports_error_without_creating_file(tmp_path):
    db_path = tmp_path / "missing.db"

    data = status.read_queue_status(db_path)

    assert data["database_exists"] is False
    assert data["database_error"] == "database file does not exist"
    assert not db_path.exists()


def test_build_dashboard_status_active_pending_and_health(db):
    running_id = q.enqueue(db, kind="start", payload=_dashboard_payload("BAS-124"), delivery_id="d-run")
    pending_id = q.enqueue(db, kind="proxy", payload=_dashboard_payload("BAS-125"), delivery_id="d-pending")
    failed_id = q.enqueue(db, kind="complete", payload=_dashboard_payload("BAS-126"), delivery_id="d-failed")

    q.pick_next_pending(db)
    _set_job_times(
        db,
        running_id,
        created_at="2026-06-19 10:00:00",
        started_at="2026-06-19 10:05:00",
    )
    _set_job_times(db, pending_id, created_at="2026-06-19 10:10:00", started_at=None)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status='failed', retries=4, last_error='token: abc123 crashed badly',
                completed_at='2026-06-19 10:20:00'
            WHERE id=?
            """,
            (failed_id,),
        )

    data = status.build_dashboard_status(db, now=datetime(2026, 6, 19, 11, 10, tzinfo=UTC))

    assert data["schema_version"] == "dashboard_v1"
    assert data["generated_at"] == "2026-06-19T11:10:00Z"
    assert data["status"] == "ok"
    assert data["counts"]["running"] == 1
    assert data["counts"]["pending"] == 1
    assert data["counts"]["failed"] == 1

    active = data["active_jobs"][0]
    assert active["job_id"] == running_id
    assert active["issue_key"] == "BAS-124"
    assert active["issue_uuid"] == "issue-124"
    assert active["title"] == "Extend status JSON"
    assert active["project"] == "Linear Executor"
    assert active["backend"] == {"cli": "opencode", "auth_mode": "oauth", "model": None}
    assert active["kind"] == "start"
    assert active["lane"] == "general"
    assert active["elapsed_seconds"] == 3900
    assert active["repo"]["status"] == "resolved"
    assert active["repo"]["path_tail"] == str(Path("bench") / "example-repo")
    assert "description" not in active
    assert "payload_json" not in active

    pending = data["pending_queue"][0]
    assert pending["job_id"] == pending_id
    assert pending["lane"] == "express"
    assert pending["queue_age_seconds"] == 3600
    assert pending["classification"] == "ready"

    assert data["health"]["executor"] == {"status": "ok"}
    assert data["health"]["stale_running_jobs"]["job_ids"] == [running_id]
    assert data["health"]["recent_failures"]["job_ids"] == [failed_id]
    assert data["health"]["queue_age"]["oldest_pending_seconds"] == 3600


def test_dashboard_status_redacts_errors_and_classifies_blocked_pending(db, monkeypatch):
    monkeypatch.delenv("LINEAR_CONTROLLER_PRODUCT_MAP", raising=False)
    job_id = q.enqueue(
        db,
        kind="start",
        payload=_dashboard_payload("BAS-127", description="repo: missing-map"),
        delivery_id="d-blocked",
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET retries=1, last_error='Authorization: Bearer secret-token failed',
                created_at='2026-06-19 10:00:00'
            WHERE id=?
            """,
            (job_id,),
        )

    data = status.build_dashboard_status(db, now=datetime(2026, 6, 19, 10, 5, tzinfo=UTC))
    pending = data["pending_queue"][0]

    assert pending["classification"] == "blocked"
    assert pending["repo"]["status"] == "error"
    assert "LINEAR_CONTROLLER_PRODUCT_MAP" in pending["repo"]["error_summary"]
    assert pending["error_summary"] == "Authorization=<redacted> failed"
    assert "secret-token" not in pending["error_summary"]


def test_status_endpoint_returns_dashboard_contract(monkeypatch, tmp_path):
    db_path = tmp_path / "jobs.db"
    monkeypatch.setenv("LINEAR_EXECUTOR_DB", str(db_path))
    app = create_app()
    q.enqueue(db_path, kind="proxy", payload=_dashboard_payload("BAS-128"), delivery_id="d-endpoint")

    response = TestClient(app).get("/status")

    assert response.status_code == 200
    data = response.json()
    assert data["schema_version"] == "dashboard_v1"
    assert data["counts"]["pending"] == 1
    assert data["pending_queue"][0]["issue_key"] == "BAS-128"
