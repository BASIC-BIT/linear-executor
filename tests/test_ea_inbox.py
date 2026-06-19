import json
import sqlite3
from datetime import UTC, datetime, timedelta

from app import ea_inbox
from app import queue as q
from app.linear_api import IssueSummary


def _issue(identifier, state, *, completed_at=None, updated_at="2026-06-19T12:00:00Z"):
    return IssueSummary(
        id=f"uuid-{identifier}",
        identifier=identifier,
        title=f"Title for {identifier}",
        url=f"https://linear.app/basicbit/issue/{identifier}/title",
        state_name=state,
        state_type="started",
        project_name="Linear Agent Control Plane",
        labels=("infrastructure",),
        created_at="2026-06-19T10:00:00Z",
        updated_at=updated_at,
        completed_at=completed_at,
    )


def _payload(identifier="BAS-117", issue_id="issue-117"):
    return {
        "data": {
            "id": issue_id,
            "identifier": identifier,
            "title": f"Title for {identifier}",
            "state": {"name": "AI Implementation"},
        }
    }


def test_build_report_classifies_human_gates_and_done_since_ack():
    now = datetime(2026, 6, 19, 13, 0, tzinfo=UTC)
    since = datetime(2026, 6, 19, 11, 0, tzinfo=UTC)
    report = ea_inbox.build_report(
        issues=[
            _issue("BAS-114", "Human Design Review"),
            _issue("BAS-124", "Done", completed_at="2026-06-19T12:30:00Z"),
            _issue("BAS-OLD", "Done", completed_at="2026-06-19T09:00:00Z"),
        ],
        jobs=[],
        since=since,
        now=now,
    )

    items = report["items"]
    assert [item["issueId"] for item in items] == ["BAS-114", "BAS-124"]
    assert items[0]["category"] == "needs BASIC action"
    assert items[0]["whyItMatters"]
    assert items[0]["recommendedAction"]
    assert items[1]["category"] == "done since last check"


def test_failed_and_stale_jobs_become_memo_items(tmp_path):
    db = tmp_path / "jobs.db"
    q.init_db(db)
    failed_id = q.enqueue(db, kind="start", payload=_payload("BAS-FAIL"), delivery_id="d-fail")
    stale_id = q.enqueue(db, kind="start", payload=_payload("BAS-STUCK"), delivery_id="d-stuck")
    q.pick_next_pending(db)
    q.mark_failed(db, failed_id, "Authorization: Bearer secret-token failed", terminal=True)
    q.pick_next_pending(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE jobs SET started_at=? WHERE id=?",
            ("2026-06-19 11:00:00", stale_id),
        )

    report = ea_inbox.build_report(
        issues=[],
        jobs=q.list_jobs(db),
        since=datetime(2026, 6, 19, 10, 0, tzinfo=UTC),
        now=datetime(2026, 6, 19, 12, 0, tzinfo=UTC),
    )

    items = report["items"]
    summaries = "\n".join(item["summary"] for item in items)
    assert "secret-token" not in summaries
    assert "Authorization=<redacted>" in summaries
    assert {item["kind"] for item in items} == {"failed_job", "stale_job"}
    assert all(item["category"] == "blocked/failing" for item in items)


def test_completed_jobs_are_aggregated_into_one_progress_memo(tmp_path):
    db = tmp_path / "jobs.db"
    q.init_db(db)
    first = q.enqueue(db, kind="start", payload=_payload("BAS-DONE-1"), delivery_id="d-1")
    second = q.enqueue(db, kind="complete", payload=_payload("BAS-DONE-2"), delivery_id="d-2")
    for job_id, completed_at in [(first, "2026-06-19 11:00:00"), (second, "2026-06-19 11:10:00")]:
        q.pick_next_pending(db)
        q.mark_done(db, job_id)
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE jobs SET completed_at=? WHERE id=?", (completed_at, job_id))

    report = ea_inbox.build_report(
        issues=[],
        jobs=q.list_jobs(db),
        since=datetime(2026, 6, 19, 10, 0, tzinfo=UTC),
        now=datetime(2026, 6, 19, 12, 0, tzinfo=UTC),
    )

    assert len(report["items"]) == 1
    item = report["items"][0]
    assert item["kind"] == "completed_jobs_summary"
    assert item["category"] == "done since last check"
    assert "2 executor job(s) completed" in item["summary"]
    assert "BAS-DONE-1" in item["summary"]
    assert "BAS-DONE-2" in item["summary"]


def test_dedupe_items_keeps_latest_duplicate():
    now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    older = ea_inbox.classify_issue(
        _issue("BAS-1", "Human Input Needed", updated_at="2026-06-19T10:00:00Z"),
        since=None,
        now=now,
    )
    newer = ea_inbox.classify_issue(
        _issue("BAS-1", "Human Input Needed", updated_at="2026-06-19T11:00:00Z"),
        since=None,
        now=now,
    )

    deduped = ea_inbox.dedupe_items([older, newer])

    assert len(deduped) == 1
    assert deduped[0].updated_at == "2026-06-19T11:00:00Z"


def test_ack_cursor_round_trip(tmp_path):
    cursor = tmp_path / "state" / "cursor.json"
    acknowledged = datetime(2026, 6, 19, 13, 30, tzinfo=UTC)

    ea_inbox.save_ack_cursor(cursor, acknowledged)

    assert ea_inbox.load_ack_cursor(cursor) == acknowledged
    payload = json.loads(cursor.read_text())
    assert payload["schemaVersion"] == "ea_inbox_cursor_v1"


def test_main_dry_run_no_linear_writes_state_and_ack(tmp_path, capsys):
    db = tmp_path / "jobs.db"
    q.init_db(db)
    q.enqueue(db, kind="start", payload=_payload("BAS-QUEUED"), delivery_id="d-queued")
    cursor = tmp_path / "cursor.json"
    last = tmp_path / "last.json"

    exit_code = ea_inbox.main([
        "--no-linear",
        "--db", str(db),
        "--cursor", str(cursor),
        "--write-state", str(last),
        "--ack",
    ])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Executive Assistant Inbox" in output
    assert "BAS-QUEUED" in output
    assert cursor.exists()
    assert last.exists()
    report = json.loads(last.read_text())
    assert report["schemaVersion"] == "ea_inbox_v1"
    assert report["items"][0]["category"] == "agent should handle next"


def test_fallback_since_uses_cursor_or_hours(tmp_path, monkeypatch):
    fixed = datetime(2026, 6, 19, 14, 0, tzinfo=UTC)
    cursor = tmp_path / "missing.json"

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(ea_inbox, "datetime", FixedDatetime)
    assert ea_inbox._default_since(cursor, 2) == fixed - timedelta(hours=2)
