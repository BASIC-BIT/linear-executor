"""Polling worker that drains the SQLite job queue.

One worker thread runs inside the Uvicorn process. It polls the queue every
``poll_interval`` seconds; when a job appears it atomically claims it (via
:func:`queue.pick_next_pending`) and dispatches to the matching orchestrator
entry point. Exceptions bubble up to :func:`queue.mark_failed`, which retries
up to ``max_retries`` before giving up.

One worker is sequential by design — Claude runs are CPU- and IO-heavy and
parallelising would overlap git worktree operations in surprising ways. If
throughput becomes a concern, run multiple workers against the same DB
(``pick_next_pending`` is atomic).
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from pathlib import Path

from app import linear_api
from app import queue as q
from app.orchestrator import TEAM_ID, orchestrate_complete, orchestrate_proxy, orchestrate_start


logger = logging.getLogger("linear-executor")


def _notify_final_failure(job: q.Job, exc: Exception) -> None:
    """When a job has exhausted retries, tell Linear about it.

    Posts a Linear comment with the last error and bounces the ticket back
    to ``Todo`` so the hung ``AI Implementation`` state is cleared. Best
    effort — any failure here is logged and swallowed (don't break the
    worker over a follow-up call).
    """
    if not job.ticket_id:
        return
    body = (
        f"⚠ **Linear-Executor — Final Failure**\n\n"
        f"Job failed after {job.max_retries + 1} attempts "
        f"({job.max_retries} retries).\n\n"
        f"Last error:\n```\n{str(exc)[:1500]}\n```\n\n"
        f"Status reset to **Todo** — fix the ticket (or its references) "
        f"and re-trigger by moving back to **AI Implementation**."
    )
    try:
        linear_api.post_comment(job.ticket_id, body)
    except Exception:
        logger.exception("worker could not post final-failure comment for %s", job.identifier)
    try:
        todo_id = linear_api.fetch_workflow_state_id(TEAM_ID, "Todo")
        if todo_id:
            linear_api.set_issue_state(job.ticket_id, todo_id)
        else:
            logger.warning("worker — could not resolve 'Todo' state id, leaving ticket in flight")
    except Exception:
        logger.exception("worker could not reset ticket state for %s", job.identifier)


def _dispatch(job: q.Job) -> None:
    payload = job.payload
    if job.kind == "start":
        orchestrate_start(payload, delivery_id=job.delivery_id)
    elif job.kind == "batch":
        orchestrate_start(payload, delivery_id=job.delivery_id, final_state="Done")
    elif job.kind == "proxy":
        orchestrate_proxy(payload, delivery_id=job.delivery_id)
    elif job.kind == "complete":
        orchestrate_complete(payload, delivery_id=job.delivery_id)
    else:
        raise ValueError(f"unknown job kind: {job.kind!r}")


def _waited_seconds(job: q.Job) -> int:
    """How long the job sat in the queue before being claimed."""
    try:
        from datetime import datetime, timezone
        # SQLite datetime('now') returns 'YYYY-MM-DD HH:MM:SS' UTC, no tz suffix
        created = datetime.strptime(job.created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return max(0, int((now - created).total_seconds()))
    except Exception:
        return 0


def _runtime_seconds(job: q.Job) -> int:
    """How long the job has been running (started_at → now)."""
    try:
        from datetime import datetime, timezone
        if not job.started_at:
            return 0
        started = datetime.strptime(job.started_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return max(0, int((now - started).total_seconds()))
    except Exception:
        return 0


def _update_status(comment_id: str | None, body: str, identifier: str) -> None:
    if not comment_id:
        return
    try:
        linear_api.update_comment(comment_id, body)
    except Exception as exc:
        logger.warning("status-comment update failed for %s: %s", identifier, exc)


def process_one(db_path: Path, kinds: list[str] | None = None, *, lane: str = "general") -> q.Job | None:
    """Claim and run the next pending job, if any. Returns the Job or None.

    ``kinds`` restricts what this call will pick up — used by the express
    lane to only drain proxy tickets.
    """
    job = q.pick_next_pending(db_path, kinds=kinds)
    if job is None:
        return None
    logger.info(
        "worker[%s] claimed job — id=%d kind=%s identifier=%s retries=%d",
        lane, job.id, job.kind, job.identifier, job.retries,
    )
    waited = _waited_seconds(job)
    _update_status(
        job.status_comment_id,
        f"🏃 **Linear-Executor** — Running\n\n"
        f"_stage: `{job.kind}` • job: {job.id} • ticket: {job.identifier} • "
        f"lane: `{lane}` • queued for {waited}s_",
        job.identifier,
    )
    try:
        _dispatch(job)
    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("worker[%s] job %d failed: %s\n%s", lane, job.id, exc, tb)
        q.mark_failed(db_path, job.id, error=f"{exc}\n{tb[-1500:]}")
        # Final failure (no more retries) → tell Linear so the ticket
        # doesn't sit silently stuck in "AI Implementation". (TES-606)
        after = q.get_job(db_path, job.id)
        if after is not None and after.status == "failed":
            _notify_final_failure(after, exc)
            _update_status(
                job.status_comment_id,
                f"❌ **Linear-Executor** — Failed\n\n"
                f"_stage: `{job.kind}` • job: {job.id} • ticket: {job.identifier} • "
                f"after {after.retries} retries — see follow-up comment for error_",
                job.identifier,
            )
        else:
            runtime = _runtime_seconds(job)
            _update_status(
                job.status_comment_id,
                f"🔁 **Linear-Executor** — Retrying\n\n"
                f"_stage: `{job.kind}` • job: {job.id} • ticket: {job.identifier} • "
                f"attempt failed after {runtime}s, will retry_",
                job.identifier,
            )
        return job
    q.mark_done(db_path, job.id)
    logger.info("worker[%s] job %d done — identifier=%s", lane, job.id, job.identifier)
    runtime = _runtime_seconds(job)
    _update_status(
        job.status_comment_id,
        f"✅ **Linear-Executor** — Done\n\n"
        f"_stage: `{job.kind}` • job: {job.id} • ticket: {job.identifier} • "
        f"finished in {runtime}s — see follow-up comment for details_",
        job.identifier,
    )
    return job


class Worker:
    """Long-lived polling worker. Safe to start/stop from a lifespan manager.

    ``kinds`` scopes the lane: pass ``None`` for the general lane (all jobs)
    or e.g. ``["proxy"]`` for the express lane that only drains proxy tickets.
    """

    def __init__(
        self,
        db_path: Path,
        poll_interval: float = 2.0,
        *,
        kinds: list[str] | None = None,
        lane: str = "general",
    ):
        self.db_path = db_path
        self.poll_interval = poll_interval
        self.kinds = kinds
        self.lane = lane
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        logger.info(
            "worker[%s] thread started — db=%s poll=%.1fs kinds=%s",
            self.lane, self.db_path, self.poll_interval, self.kinds or "ALL",
        )
        while not self._stop_event.is_set():
            try:
                picked = process_one(self.db_path, kinds=self.kinds, lane=self.lane)
            except Exception:
                # Defensive: log but never let the thread die
                logger.exception("worker[%s] loop swallowed unexpected error", self.lane)
                picked = None
            if picked is None:
                # Idle — sleep for poll_interval but wake up early on stop
                self._stop_event.wait(self.poll_interval)
            # If we did work, immediately check for more without sleeping
        logger.info("worker[%s] thread stopped", self.lane)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"linear-executor-{self.lane}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
