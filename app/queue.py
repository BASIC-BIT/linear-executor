"""SQLite-backed job queue for the linear-executor.

Webhook handler enqueues a job and returns 200 immediately; a background
worker thread pulls jobs in FIFO order and dispatches to the appropriate
orchestrator entry point. Survives process restarts (WAL mode, atomic picks).

Schema
------
``jobs(id, ticket_id, identifier, delivery_id, kind, payload_json,
       status, retries, max_retries, last_error,
       created_at, started_at, completed_at)``

Statuses: ``pending`` → ``running`` → ``done | failed | cancelled``. A
``failed`` with ``retries < max_retries`` is flipped back to ``pending``.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    identifier TEXT NOT NULL,
    delivery_id TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('start', 'proxy', 'complete', 'batch')),
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'running', 'done', 'failed', 'cancelled')),
    retries INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    last_error TEXT,
    status_comment_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_ticket ON jobs(ticket_id);
"""

_MIGRATIONS = (
    "ALTER TABLE jobs ADD COLUMN status_comment_id TEXT",
)


@dataclass(frozen=True)
class Job:
    id: int
    ticket_id: str
    identifier: str
    delivery_id: str | None
    kind: str
    payload_json: str
    status: str
    retries: int
    max_retries: int
    last_error: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None
    status_comment_id: str | None = None

    @property
    def payload(self) -> dict:
        return json.loads(self.payload_json)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists


def _row_to_job(row: sqlite3.Row) -> Job:
    keys = row.keys()
    return Job(
        id=row["id"],
        ticket_id=row["ticket_id"],
        identifier=row["identifier"],
        delivery_id=row["delivery_id"],
        kind=row["kind"],
        payload_json=row["payload_json"],
        status=row["status"],
        retries=row["retries"],
        max_retries=row["max_retries"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        status_comment_id=row["status_comment_id"] if "status_comment_id" in keys else None,
    )


def enqueue(
    db_path: Path,
    *,
    kind: str,
    payload: dict[str, Any],
    delivery_id: str | None,
    max_retries: int = 3,
) -> int:
    if kind not in ("start", "proxy", "complete", "batch"):
        raise ValueError(f"unknown kind: {kind!r}")
    data = payload.get("data") or {}
    ticket_id = data.get("id") or ""
    identifier = data.get("identifier") or "?"
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO jobs (ticket_id, identifier, delivery_id, kind, payload_json, max_retries)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ticket_id, identifier, delivery_id, kind, json.dumps(payload), max_retries),
        )
        return int(cur.lastrowid)


def get_job(db_path: Path, job_id: int) -> Job | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def set_status_comment_id(db_path: Path, job_id: int, comment_id: str) -> None:
    """Record the Linear comment id used as the live status indicator for this job."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET status_comment_id=? WHERE id=?",
            (comment_id, job_id),
        )


def pick_next_pending(db_path: Path, kinds: list[str] | None = None) -> Job | None:
    """Atomically claim the oldest pending job and mark it ``running``.

    Uses ``UPDATE ... RETURNING`` so two concurrent workers can't both claim
    the same row. Returns ``None`` if nothing is pending.

    ``kinds`` restricts the claim to a whitelist, e.g. ``["proxy"]`` for the
    express lane. ``None`` means no filter (the general lane).
    """
    if kinds is None:
        query = """
            UPDATE jobs
            SET status = 'running', started_at = datetime('now')
            WHERE id = (
                SELECT id FROM jobs WHERE status = 'pending'
                ORDER BY created_at ASC, id ASC LIMIT 1
            )
            RETURNING *
        """
        params: tuple = ()
    else:
        placeholders = ",".join("?" for _ in kinds)
        query = f"""
            UPDATE jobs
            SET status = 'running', started_at = datetime('now')
            WHERE id = (
                SELECT id FROM jobs
                WHERE status = 'pending' AND kind IN ({placeholders})
                ORDER BY created_at ASC, id ASC LIMIT 1
            )
            RETURNING *
        """
        params = tuple(kinds)
    with _connect(db_path) as conn:
        row = conn.execute(query, params).fetchone()
    return _row_to_job(row) if row else None


def mark_done(db_path: Path, job_id: int) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET status='done', completed_at=datetime('now') WHERE id=?",
            (job_id,),
        )


def mark_failed(db_path: Path, job_id: int, error: str) -> None:
    """Increment retries; back to ``pending`` if under max, else ``failed``."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT retries, max_retries FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            return
        new_retries = row["retries"] + 1
        if new_retries > row["max_retries"]:
            conn.execute(
                """
                UPDATE jobs
                SET status='failed', retries=?, last_error=?,
                    completed_at=datetime('now')
                WHERE id=?
                """,
                (new_retries, error, job_id),
            )
        else:
            conn.execute(
                """
                UPDATE jobs
                SET status='pending', retries=?, last_error=?, started_at=NULL
                WHERE id=?
                """,
                (new_retries, error, job_id),
            )


def mark_cancelled_for_ticket(db_path: Path, ticket_id: str) -> int:
    """Cancel every pending or running job tied to a Linear ticket id."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE jobs
            SET status='cancelled', completed_at=datetime('now')
            WHERE ticket_id=? AND status IN ('pending', 'running')
            """,
            (ticket_id,),
        )
        return cur.rowcount


def has_cancelled_job(db_path: Path, ticket_id: str) -> bool:
    """True if any job for this ticket is in ``cancelled`` state.

    Used by orchestrate_start / orchestrate_proxy after run_claude returns
    to decide whether to skip post-run side-effects (state→Draft PR Ready,
    run-comment) — the cancel handler already posted a comment and
    flipped the queue status. See TES-596 edge-case.
    """
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM jobs
            WHERE ticket_id=? AND status='cancelled'
            LIMIT 1
            """,
            (ticket_id,),
        ).fetchone()
    return row is not None


def list_active_for_ticket(db_path: Path, ticket_id: str) -> list[Job]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE ticket_id=? AND status IN ('pending', 'running')
            ORDER BY created_at ASC
            """,
            (ticket_id,),
        ).fetchall()
    return [_row_to_job(r) for r in rows]


def reset_stale_running(db_path: Path) -> int:
    """Boot-time recovery: any row left as ``running`` (because the previous
    process was killed mid-execution) is returned to the queue as pending so
    the new worker can pick it up. Returns the number of rows reset.
    """
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='pending', started_at=NULL WHERE status='running'"
        )
        return cur.rowcount
