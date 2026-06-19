import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import queue as q
from app.main import create_app
from app.status import build_status


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "jobs.db"
    q.init_db(path)
    return path


def _payload(identifier="BAS-124", issue_id="issue-124", *, description="folder: D:/bench/example-repo"):
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


def test_build_status_dashboard_v1_active_pending_and_health(db):
    running_id = q.enqueue(db, kind="start", payload=_payload("BAS-124"), delivery_id="d-run")
    pending_id = q.enqueue(db, kind="proxy", payload=_payload("BAS-125"), delivery_id="d-pending")
    failed_id = q.enqueue(db, kind="complete", payload=_payload("BAS-126"), delivery_id="d-failed")

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

    status = build_status(db, now=datetime(2026, 6, 19, 11, 10, tzinfo=UTC))

    assert status["schema_version"] == "dashboard_v1"
    assert status["generated_at"] == "2026-06-19T11:10:00Z"
    assert status["status"] == "ok"
    assert status["counts"]["running"] == 1
    assert status["counts"]["pending"] == 1
    assert status["counts"]["failed"] == 1

    active = status["active_jobs"][0]
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

    pending = status["pending_queue"][0]
    assert pending["job_id"] == pending_id
    assert pending["lane"] == "express"
    assert pending["queue_age_seconds"] == 3600
    assert pending["classification"] == "ready"

    assert status["health"]["executor"] == {"status": "ok"}
    assert status["health"]["stale_running_jobs"]["job_ids"] == [running_id]
    assert status["health"]["recent_failures"]["job_ids"] == [failed_id]
    assert status["health"]["queue_age"]["oldest_pending_seconds"] == 3600


def test_status_redacts_errors_and_classifies_blocked_pending(db, monkeypatch):
    monkeypatch.delenv("LINEAR_CONTROLLER_PRODUCT_MAP", raising=False)
    job_id = q.enqueue(
        db,
        kind="start",
        payload=_payload("BAS-127", description="repo: missing-map"),
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

    status = build_status(db, now=datetime(2026, 6, 19, 10, 5, tzinfo=UTC))
    pending = status["pending_queue"][0]

    assert pending["classification"] == "blocked"
    assert pending["repo"]["status"] == "error"
    assert "LINEAR_CONTROLLER_PRODUCT_MAP" in pending["repo"]["error_summary"]
    assert pending["error_summary"] == "Authorization=<redacted> failed"
    assert "secret-token" not in pending["error_summary"]


def test_status_endpoint_returns_dashboard_contract(monkeypatch, tmp_path):
    db_path = tmp_path / "jobs.db"
    monkeypatch.setenv("LINEAR_EXECUTOR_DB", str(db_path))
    app = create_app()
    q.enqueue(db_path, kind="proxy", payload=_payload("BAS-128"), delivery_id="d-endpoint")

    response = TestClient(app).get("/status")

    assert response.status_code == 200
    data = response.json()
    assert data["schema_version"] == "dashboard_v1"
    assert data["counts"]["pending"] == 1
    assert data["pending_queue"][0]["issue_key"] == "BAS-128"
