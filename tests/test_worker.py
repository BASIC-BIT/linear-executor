"""Tests for app/worker.py — the polling worker that consumes the SQLite queue."""
import threading
import time

import pytest

from app import queue as q
from app import worker as w


@pytest.fixture(autouse=True)
def _stub_linear_api(monkeypatch):
    """Block real Linear-API traffic from the final-failure notifier.

    Tests that want to verify _notify_final_failure was called can override
    these by re-patching ``app.worker._notify_final_failure`` or the linear_api
    members directly.
    """
    monkeypatch.setattr("app.worker.linear_api.post_comment", lambda *a, **kw: "stub-comment-id")
    monkeypatch.setattr("app.worker.linear_api.fetch_workflow_state_id", lambda *a, **kw: "stub-state-id")
    monkeypatch.setattr("app.worker.linear_api.set_issue_state", lambda *a, **kw: True)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "jobs.db"
    q.init_db(path)
    return path


def _payload(identifier="TES-800", issue_id="iss-800"):
    return {
        "action": "update",
        "type": "Issue",
        "data": {
            "id": issue_id,
            "identifier": identifier,
            "title": "T",
            "description": "",
            "state": {"name": "AI Implementation"},
        },
    }


def test_worker_dispatches_start_to_orchestrate_start(db, monkeypatch):
    calls = []

    def fake_start(payload, delivery_id=None):
        calls.append(("start", payload["data"]["identifier"], delivery_id))

    monkeypatch.setattr("app.worker.orchestrate_start", fake_start)
    monkeypatch.setattr("app.worker.orchestrate_proxy", lambda *a, **kw: calls.append(("proxy", a, kw)))
    monkeypatch.setattr("app.worker.orchestrate_complete", lambda *a, **kw: calls.append(("complete", a, kw)))
    monkeypatch.setattr("app.worker.orchestrate_review_watch", lambda *a, **kw: calls.append(("review_watch", a, kw)))

    jid = q.enqueue(db, kind="start", payload=_payload("TES-801"), delivery_id="d-801")
    w.process_one(db)

    assert calls == [("start", "TES-801", "d-801")]
    job = q.get_job(db, jid)
    assert job.status == "done"


def test_worker_dispatches_proxy_and_complete_to_right_handler(db, monkeypatch):
    calls = []
    monkeypatch.setattr("app.worker.orchestrate_start", lambda *a, **kw: calls.append(("start",)))
    monkeypatch.setattr("app.worker.orchestrate_proxy", lambda payload, delivery_id=None: calls.append(("proxy", payload["data"]["identifier"])))
    monkeypatch.setattr("app.worker.orchestrate_complete", lambda payload, delivery_id=None: calls.append(("complete", payload["data"]["identifier"])))
    monkeypatch.setattr("app.worker.orchestrate_review_watch", lambda payload, delivery_id=None: calls.append(("review_watch", payload["data"]["identifier"])))

    q.enqueue(db, kind="proxy", payload=_payload("TES-P1", issue_id="iss-p1"), delivery_id="p1")
    q.enqueue(db, kind="complete", payload=_payload("TES-C1", issue_id="iss-c1"), delivery_id="c1")
    q.enqueue(db, kind="review_watch", payload=_payload("TES-RW1", issue_id="iss-rw1"), delivery_id="rw1")

    w.process_one(db)
    w.process_one(db)
    w.process_one(db)

    assert calls == [("proxy", "TES-P1"), ("complete", "TES-C1"), ("review_watch", "TES-RW1")]


def test_worker_retries_on_exception_then_fails(db, monkeypatch):
    attempts = {"count": 0}

    def flaky(payload, delivery_id=None):
        attempts["count"] += 1
        raise RuntimeError("transient failure")

    monkeypatch.setattr("app.worker.orchestrate_start", flaky)
    monkeypatch.setattr("app.worker.orchestrate_proxy", lambda *a, **kw: None)
    monkeypatch.setattr("app.worker.orchestrate_complete", lambda *a, **kw: None)
    monkeypatch.setattr("app.worker.orchestrate_review_watch", lambda *a, **kw: None)

    jid = q.enqueue(db, kind="start", payload=_payload("TES-R1"), delivery_id="r1", max_retries=2)

    # 1st attempt → fails, retries=1, back to pending
    w.process_one(db)
    assert q.get_job(db, jid).status == "pending"
    assert q.get_job(db, jid).retries == 1

    # 2nd → retries=2, still under/equal max, back to pending
    w.process_one(db)
    assert q.get_job(db, jid).status == "pending"
    assert q.get_job(db, jid).retries == 2

    # 3rd → retries=3 > max_retries=2 → failed
    w.process_one(db)
    job = q.get_job(db, jid)
    assert job.status == "failed"
    assert job.retries == 3
    assert "transient failure" in (job.last_error or "")
    assert attempts["count"] == 3


def test_final_failure_posts_linear_comment_and_resets_state(db, monkeypatch):
    """After max_retries are exhausted, the worker tells Linear about it
    and moves the ticket to Human Input Needed so it doesn't hang on an AI lane."""
    posted = []
    state_changes = []

    monkeypatch.setattr(
        "app.worker.linear_api.post_comment",
        lambda issue_id, body, client=None: (posted.append((issue_id, body)) or "c-id")[-1],
    )
    monkeypatch.setattr(
        "app.worker.linear_api.fetch_workflow_state_id",
        lambda team, name, client=None: f"state-{name}",
    )
    monkeypatch.setattr(
        "app.worker.linear_api.set_issue_state",
        lambda issue_id, state_id, client=None: (state_changes.append((issue_id, state_id)) or True)[-1],
    )

    def always_fail(payload, delivery_id=None):
        raise RuntimeError("permanent failure")
    monkeypatch.setattr("app.worker.orchestrate_start", always_fail)
    monkeypatch.setattr("app.worker.orchestrate_proxy", lambda *a, **kw: None)
    monkeypatch.setattr("app.worker.orchestrate_complete", lambda *a, **kw: None)
    monkeypatch.setattr("app.worker.orchestrate_review_watch", lambda *a, **kw: None)

    jid = q.enqueue(db, kind="start", payload=_payload("TES-FAIL", issue_id="iss-fail"), delivery_id="f1", max_retries=1)

    # 1st: retries=1 (under max=1? actually 1 > 1 is false, so stays pending? Let's check…)
    # Actually mark_failed: new_retries=1, > max_retries=1? NO. So back to pending.
    w.process_one(db)
    assert q.get_job(db, jid).status == "pending"
    assert posted == []  # not yet final

    # 2nd: new_retries=2 > max_retries=1 → final failed
    w.process_one(db)
    job = q.get_job(db, jid)
    assert job.status == "failed"

    assert len(posted) == 1
    issue_id, body = posted[0]
    assert issue_id == "iss-fail"
    assert "Final Failure" in body
    assert "permanent failure" in body

    assert "Human Input Needed" in body
    assert state_changes == [("iss-fail", "state-Human Input Needed")]


def test_final_failure_swallows_linear_errors(db, monkeypatch):
    """If Linear is down when we try to notify, the worker keeps running."""
    monkeypatch.setattr(
        "app.worker.linear_api.post_comment",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("linear is down")),
    )
    monkeypatch.setattr(
        "app.worker.linear_api.fetch_workflow_state_id",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("linear still down")),
    )

    def always_fail(payload, delivery_id=None):
        raise RuntimeError("inner")
    monkeypatch.setattr("app.worker.orchestrate_start", always_fail)
    monkeypatch.setattr("app.worker.orchestrate_proxy", lambda *a, **kw: None)
    monkeypatch.setattr("app.worker.orchestrate_complete", lambda *a, **kw: None)
    monkeypatch.setattr("app.worker.orchestrate_review_watch", lambda *a, **kw: None)

    jid = q.enqueue(db, kind="start", payload=_payload("TES-FAIL2", issue_id="iss-fail2"), delivery_id="f2", max_retries=0)

    # max_retries=0 → first failure is final
    w.process_one(db)
    assert q.get_job(db, jid).status == "failed"
    # If Linear errors had bubbled up the worker would have crashed; reaching
    # this assertion means the swallow worked.


def test_worker_skips_cancelled_jobs(db, monkeypatch):
    calls = []
    monkeypatch.setattr("app.worker.orchestrate_start", lambda *a, **kw: calls.append("ran"))
    monkeypatch.setattr("app.worker.orchestrate_proxy", lambda *a, **kw: None)
    monkeypatch.setattr("app.worker.orchestrate_complete", lambda *a, **kw: None)
    monkeypatch.setattr("app.worker.orchestrate_review_watch", lambda *a, **kw: None)

    jid = q.enqueue(db, kind="start", payload=_payload("TES-C2"), delivery_id="c2")
    q.mark_cancelled_for_ticket(db, ticket_id="iss-800")  # payload default issue_id

    result = w.process_one(db)
    assert result is None  # nothing to pick
    assert calls == []
    assert q.get_job(db, jid).status == "cancelled"


def test_process_one_returns_none_when_queue_empty(db, monkeypatch):
    monkeypatch.setattr("app.worker.orchestrate_start", lambda *a, **kw: None)
    monkeypatch.setattr("app.worker.orchestrate_proxy", lambda *a, **kw: None)
    monkeypatch.setattr("app.worker.orchestrate_complete", lambda *a, **kw: None)
    monkeypatch.setattr("app.worker.orchestrate_review_watch", lambda *a, **kw: None)

    assert w.process_one(db) is None


def test_express_worker_only_picks_proxy_jobs(db, monkeypatch):
    """Express-lane worker leaves start/complete jobs for the general worker."""
    calls = []
    monkeypatch.setattr("app.worker.orchestrate_start", lambda p, delivery_id=None: calls.append(("start", p["data"]["identifier"])))
    monkeypatch.setattr("app.worker.orchestrate_proxy", lambda p, delivery_id=None: calls.append(("proxy", p["data"]["identifier"])))
    monkeypatch.setattr("app.worker.orchestrate_complete", lambda *a, **kw: None)
    monkeypatch.setattr("app.worker.orchestrate_review_watch", lambda *a, **kw: None)

    j_start = q.enqueue(db, kind="start", payload=_payload("TES-EX1", issue_id="iss-ex1"), delivery_id="ex1")
    j_proxy = q.enqueue(db, kind="proxy", payload=_payload("TES-EX2", issue_id="iss-ex2"), delivery_id="ex2")

    # Express pass: claims only proxy
    picked_express = w.process_one(db, kinds=["proxy"], lane="express")
    assert picked_express.id == j_proxy
    # Second express pass — no proxy left, returns None
    assert w.process_one(db, kinds=["proxy"], lane="express") is None

    # General pass: claims the leftover start
    picked_general = w.process_one(db, lane="general")
    assert picked_general.id == j_start

    assert calls == [("proxy", "TES-EX2"), ("start", "TES-EX1")]
    assert q.get_job(db, j_start).status == "done"
    assert q.get_job(db, j_proxy).status == "done"


def test_two_lane_workers_run_parallel_without_double_claim(db, monkeypatch):
    """Both lanes running against the same DB never process the same job twice."""
    calls = []
    lock_calls = threading.Lock()

    def fake_start(p, delivery_id=None):
        with lock_calls:
            calls.append(("start", p["data"]["identifier"]))
        time.sleep(0.05)

    def fake_proxy(p, delivery_id=None):
        with lock_calls:
            calls.append(("proxy", p["data"]["identifier"]))
        time.sleep(0.05)

    monkeypatch.setattr("app.worker.orchestrate_start", fake_start)
    monkeypatch.setattr("app.worker.orchestrate_proxy", fake_proxy)
    monkeypatch.setattr("app.worker.orchestrate_complete", lambda *a, **kw: None)
    monkeypatch.setattr("app.worker.orchestrate_review_watch", lambda *a, **kw: None)

    general = w.Worker(db, poll_interval=0.05, lane="general")
    express = w.Worker(db, poll_interval=0.05, kinds=["proxy"], lane="express")
    general.start()
    express.start()
    try:
        # Mix 2 proxy + 2 start — total 4 jobs
        q.enqueue(db, kind="proxy", payload=_payload("P1", issue_id="p1"), delivery_id="p1")
        q.enqueue(db, kind="start", payload=_payload("S1", issue_id="s1"), delivery_id="s1")
        q.enqueue(db, kind="proxy", payload=_payload("P2", issue_id="p2"), delivery_id="p2")
        q.enqueue(db, kind="start", payload=_payload("S2", issue_id="s2"), delivery_id="s2")
        deadline = time.time() + 5.0
        while time.time() < deadline and len(calls) < 4:
            time.sleep(0.05)
    finally:
        general.stop()
        express.stop()

    # All 4 jobs ran exactly once
    assert len(calls) == 4
    identifiers_called = [c[1] for c in calls]
    assert sorted(identifiers_called) == ["P1", "P2", "S1", "S2"]


def test_worker_thread_consumes_queue_and_can_be_stopped(db, monkeypatch):
    """Integration: start the thread, enqueue 2 jobs, verify they run, then stop."""
    calls = []

    def fake_start(payload, delivery_id=None):
        calls.append(payload["data"]["identifier"])

    monkeypatch.setattr("app.worker.orchestrate_start", fake_start)
    monkeypatch.setattr("app.worker.orchestrate_proxy", lambda *a, **kw: None)
    monkeypatch.setattr("app.worker.orchestrate_complete", lambda *a, **kw: None)
    monkeypatch.setattr("app.worker.orchestrate_review_watch", lambda *a, **kw: None)

    worker = w.Worker(db, poll_interval=0.05)
    worker.start()
    try:
        q.enqueue(db, kind="start", payload=_payload("TES-T1", issue_id="iss-t1"), delivery_id="t1")
        q.enqueue(db, kind="start", payload=_payload("TES-T2", issue_id="iss-t2"), delivery_id="t2")
        # wait up to 3s for both
        deadline = time.time() + 3.0
        while time.time() < deadline and len(calls) < 2:
            time.sleep(0.05)
    finally:
        worker.stop()

    assert sorted(calls) == ["TES-T1", "TES-T2"]
