"""Tests for the SQLite-backed job queue (app/queue.py)."""
import json

import pytest

from app import queue as q


@pytest.fixture
def db(tmp_path):
    """Per-test fresh queue file."""
    path = tmp_path / "jobs.db"
    q.init_db(path)
    return path


def _sample_payload(identifier="TES-700", issue_id="issue-uuid"):
    return {
        "data": {
            "id": issue_id,
            "identifier": identifier,
            "title": "Hello",
            "description": "body",
            "state": {"name": "AI Implementation"},
        }
    }


def test_enqueue_creates_pending_job(db):
    job_id = q.enqueue(db, kind="start", payload=_sample_payload(), delivery_id="d-1")
    job = q.get_job(db, job_id)
    assert job.status == "pending"
    assert job.kind == "start"
    assert job.identifier == "TES-700"
    assert job.ticket_id == "issue-uuid"
    assert job.delivery_id == "d-1"
    assert job.retries == 0
    assert json.loads(job.payload_json)["data"]["identifier"] == "TES-700"


def test_pick_next_pending_returns_oldest_and_flips_to_running(db):
    j1 = q.enqueue(db, kind="start", payload=_sample_payload("TES-1"), delivery_id="d-1")
    j2 = q.enqueue(db, kind="start", payload=_sample_payload("TES-2"), delivery_id="d-2")
    picked = q.pick_next_pending(db)
    assert picked.id == j1
    assert picked.status == "running"
    assert picked.started_at is not None
    # second pick gets j2
    picked2 = q.pick_next_pending(db)
    assert picked2.id == j2
    # third pick returns None
    assert q.pick_next_pending(db) is None


def test_pick_is_atomic_across_two_calls(db):
    """Same job cannot be picked twice even under rapid consecutive calls."""
    q.enqueue(db, kind="start", payload=_sample_payload("TES-X"), delivery_id="d-x")
    first = q.pick_next_pending(db)
    second = q.pick_next_pending(db)
    assert first is not None
    assert second is None


def test_mark_done_updates_status_and_timestamp(db):
    jid = q.enqueue(db, kind="proxy", payload=_sample_payload("TES-3"), delivery_id="d-3")
    q.pick_next_pending(db)
    q.mark_done(db, jid)
    job = q.get_job(db, jid)
    assert job.status == "done"
    assert job.completed_at is not None
    assert job.last_error is None


def test_mark_failed_retries_below_max_return_to_pending(db):
    jid = q.enqueue(db, kind="start", payload=_sample_payload("TES-4"), delivery_id="d-4")
    q.pick_next_pending(db)
    q.mark_failed(db, jid, error="boom")
    job = q.get_job(db, jid)
    assert job.status == "pending"
    assert job.retries == 1
    assert job.last_error == "boom"
    assert job.started_at is None  # cleared for retry


def test_mark_failed_at_max_retries_goes_to_failed(db):
    jid = q.enqueue(db, kind="start", payload=_sample_payload("TES-5"), delivery_id="d-5", max_retries=2)
    # cycle: pick → fail → pick → fail → pick → fail
    for i in range(3):
        q.pick_next_pending(db)
        q.mark_failed(db, jid, error=f"err-{i}")
    job = q.get_job(db, jid)
    assert job.status == "failed"
    assert job.retries == 3
    assert job.last_error == "err-2"
    assert job.completed_at is not None


def test_mark_cancelled_for_ticket_affects_pending_and_running(db):
    j_pending = q.enqueue(db, kind="start", payload=_sample_payload("TES-6", issue_id="iss-6"), delivery_id="d-6")
    j_running = q.enqueue(db, kind="start", payload=_sample_payload("TES-6", issue_id="iss-6"), delivery_id="d-6b")
    q.pick_next_pending(db)  # flips j_pending first → running; keep going
    # Second call picks j_running (but both are tied to iss-6 for this test)
    count = q.mark_cancelled_for_ticket(db, ticket_id="iss-6")
    assert count == 2
    assert q.get_job(db, j_pending).status == "cancelled"
    assert q.get_job(db, j_running).status == "cancelled"


def test_mark_cancelled_leaves_done_jobs_alone(db):
    j_done = q.enqueue(db, kind="start", payload=_sample_payload("TES-7", issue_id="iss-7"), delivery_id="d-7")
    q.pick_next_pending(db)
    q.mark_done(db, j_done)
    count = q.mark_cancelled_for_ticket(db, ticket_id="iss-7")
    assert count == 0
    assert q.get_job(db, j_done).status == "done"


def test_persistence_across_connections(db):
    """Queue state survives process restart (we simulate by reopening)."""
    jid = q.enqueue(db, kind="complete", payload=_sample_payload("TES-8"), delivery_id="d-8")
    # Fresh "connection" — same file
    job = q.get_job(db, jid)
    assert job.kind == "complete"
    assert job.status == "pending"


def test_list_active_for_ticket_returns_pending_and_running(db):
    q.enqueue(db, kind="start", payload=_sample_payload("TES-9", issue_id="iss-9"), delivery_id="d-9")
    q.enqueue(db, kind="start", payload=_sample_payload("TES-9", issue_id="iss-9"), delivery_id="d-9b")
    q.pick_next_pending(db)  # one running, one pending
    active = q.list_active_for_ticket(db, ticket_id="iss-9")
    assert len(active) == 2
    statuses = {j.status for j in active}
    assert statuses == {"running", "pending"}


def test_pick_with_kinds_filter_skips_other_kinds(db):
    """Express lane: kinds=['proxy'] must leave start/complete jobs alone."""
    q.enqueue(db, kind="start", payload=_sample_payload("TES-K1"), delivery_id="k1")
    q.enqueue(db, kind="proxy", payload=_sample_payload("TES-K2"), delivery_id="k2")
    q.enqueue(db, kind="complete", payload=_sample_payload("TES-K3"), delivery_id="k3")

    # Express picks only the proxy job even though start was enqueued first
    picked = q.pick_next_pending(db, kinds=["proxy"])
    assert picked.identifier == "TES-K2"
    assert picked.kind == "proxy"

    # No more proxy jobs → None
    assert q.pick_next_pending(db, kinds=["proxy"]) is None

    # General picks the oldest leftover (the start job)
    picked2 = q.pick_next_pending(db)
    assert picked2.identifier == "TES-K1"
    assert picked2.kind == "start"


def test_stale_running_reset_to_pending(db):
    """Boot-time recovery: running jobs left by a crashed worker go back to pending."""
    jid = q.enqueue(db, kind="start", payload=_sample_payload("TES-10"), delivery_id="d-10")
    q.pick_next_pending(db)
    assert q.get_job(db, jid).status == "running"
    # simulate crash + restart
    count = q.reset_stale_running(db)
    assert count == 1
    job = q.get_job(db, jid)
    assert job.status == "pending"
    assert job.started_at is None
