"""Tests for the cancel flow (TES-596).

Covers three scenarios:

1. Active running subprocess + queued job → terminate process, mark queue,
   post comment with had_running=True wording.
2. Only queue jobs (no live subprocess) → mark cancelled, post comment
   with no-running wording.
3. Nothing to cancel → comment says so, no exceptions.
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from app import cancel, job_registry, queue as q


@pytest.fixture(autouse=True)
def _reset_registry():
    job_registry.clear()
    yield
    job_registry.clear()


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "jobs.db"
    q.init_db(db)
    return db


def _make_long_subprocess() -> subprocess.Popen:
    """Spawn a real ``sleep 60`` so we can verify SIGTERM lands."""
    return subprocess.Popen(
        ["sleep", "60"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def test_cancel_with_active_subprocess_terminates_and_marks_queue(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    payload = {"data": {"id": "iss-1", "identifier": "TES-9001",
                        "state": {"name": "AI Implementation"}, "title": "x", "description": "y"}}
    q.enqueue(db, kind="start", payload=payload, delivery_id="d1")

    proc = _make_long_subprocess()
    job_registry.register("TES-9001", proc)

    posted = []
    monkeypatch.setattr("app.cancel.linear_api.post_comment",
                        lambda issue_id, body: posted.append((issue_id, body)))

    result = cancel.cancel_ticket(db, "TES-9001", "iss-1", sync=True)

    assert result["had_running_process"] is True
    assert result["cancelled_jobs"] >= 1
    assert proc.poll() is not None, "subprocess should have been terminated"
    assert len(posted) == 1
    assert posted[0][0] == "iss-1"
    assert "Run aborted" in posted[0][1]
    assert "⏸ **Linear-Executor**" in posted[0][1]


def test_cancel_with_only_queued_jobs_no_live_process(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    payload = {"data": {"id": "iss-2", "identifier": "TES-9002",
                        "state": {"name": "AI Implementation"}, "title": "x", "description": "y"}}
    q.enqueue(db, kind="start", payload=payload, delivery_id="d2")

    posted = []
    monkeypatch.setattr("app.cancel.linear_api.post_comment",
                        lambda issue_id, body: posted.append((issue_id, body)))

    result = cancel.cancel_ticket(db, "TES-9002", "iss-2", sync=True)

    assert result["had_running_process"] is False
    assert result["cancelled_jobs"] >= 1
    assert "No active subprocess" in posted[0][1]


def test_cancel_with_nothing_to_cancel_still_posts_friendly_comment(monkeypatch, tmp_path):
    db = _make_db(tmp_path)

    posted = []
    monkeypatch.setattr("app.cancel.linear_api.post_comment",
                        lambda issue_id, body: posted.append((issue_id, body)))

    result = cancel.cancel_ticket(db, "TES-9003", "iss-3", sync=True)

    assert result["had_running_process"] is False
    assert result["cancelled_jobs"] == 0
    assert "nothing to cancel" in posted[0][1].lower()


def test_cancel_unregisters_after_termination(monkeypatch, tmp_path):
    db = _make_db(tmp_path)
    proc = _make_long_subprocess()
    job_registry.register("TES-9004", proc)

    monkeypatch.setattr("app.cancel.linear_api.post_comment", lambda *a, **kw: None)

    cancel.cancel_ticket(db, "TES-9004", "iss-4", sync=True)

    assert job_registry.get("TES-9004") is None


def test_cancel_async_thread_doesnt_block(monkeypatch, tmp_path):
    """Default async path: cancel_ticket returns immediately, even with
    a long-grace SIGKILL fallback."""
    db = _make_db(tmp_path)
    proc = _make_long_subprocess()
    job_registry.register("TES-9005", proc)

    monkeypatch.setattr("app.cancel.linear_api.post_comment", lambda *a, **kw: None)

    import time
    t0 = time.monotonic()
    result = cancel.cancel_ticket(db, "TES-9005", "iss-5")  # sync=False
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, f"cancel_ticket should return immediately, took {elapsed:.2f}s"
    assert result["had_running_process"] is True

    # Give the daemon thread time to terminate the process before test exit
    proc.wait(timeout=10)
    job_registry.unregister("TES-9005")  # async path didn't unregister yet, do it manually
