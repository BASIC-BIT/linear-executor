"""Cancel an in-flight Claude run when a Linear ticket transitions to Canceled.

Three things happen on cancel (TES-596):

1. Queue jobs for this ticket are flagged ``cancelled`` so the worker
   skips any not-yet-claimed job.
2. The currently running Popen for this ticket (if any) is terminated —
   SIGTERM first, then SIGKILL after a grace period — taking the whole
   process group down (orchestrator spawned with ``start_new_session=True``).
3. A short ``HEADER_CANCELLED`` comment is posted so the user sees that
   the cancel actually happened, not just that they clicked something.

The Linear status stays on Canceled. We deliberately don't bounce it back
to Todo — re-running an aborted task is an explicit user decision.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from app import job_registry
from app import linear_api
from app import queue as q
from app.runner import terminate_process_tree


logger = logging.getLogger("linear-executor")

HEADER_CANCELLED = "⏸ **Linear-Executor** — Run Cancelled"

GRACE_BEFORE_KILL_SECONDS = 5


def _terminate_with_grace(ticket_id: str, popen) -> None:
    """SIGTERM, wait grace_seconds, then SIGKILL if still alive.

    Runs in a daemon thread so the webhook handler can return immediately.
    """
    pid = popen.pid
    try:
        terminate_process_tree(popen, force=False)
        logger.info("cancel — ticket=%s terminate sent to process tree pid=%d", ticket_id, pid)
    except Exception as exc:
        logger.warning("cancel — ticket=%s terminate failed: %s", ticket_id, exc)
        return

    try:
        popen.wait(timeout=GRACE_BEFORE_KILL_SECONDS)
        logger.info("cancel — ticket=%s pid=%d exited gracefully", ticket_id, pid)
    except Exception:
        logger.warning(
            "cancel — ticket=%s pid=%d still alive after %ds, sending SIGKILL",
            ticket_id, pid, GRACE_BEFORE_KILL_SECONDS,
        )
        try:
            terminate_process_tree(popen, force=True)
        except Exception as exc:
            logger.error("cancel — ticket=%s SIGKILL failed: %s", ticket_id, exc)
    finally:
        job_registry.unregister(ticket_id)


def cancel_ticket(
    db_path: Path,
    ticket_id: str,
    issue_id: str | None,
    *,
    sync: bool = False,
) -> dict:
    """Mark queue jobs cancelled, kill running Popen, post a comment.

    Args:
        db_path: queue database
        ticket_id: human identifier (e.g. ``"TES-595"``)
        issue_id: Linear issue UUID for the comment post (if known)
        sync: when True, run termination synchronously (only for tests).
            Default False: terminate in a daemon thread so the webhook
            handler returns immediately.

    Returns dict with ``cancelled_jobs`` count and ``had_running_process``
    flag for logging / tests.

    Note: queue rows are keyed by Linear UUID (``data.id``), not identifier
    string. ``ticket_id`` in our registry uses the human identifier; we
    pass ``issue_id`` (UUID) to ``mark_cancelled_for_ticket`` which is what
    ``enqueue`` stored as the queue's ``ticket_id`` column.
    """
    cancelled_jobs = q.mark_cancelled_for_ticket(db_path, issue_id or "") if issue_id else 0
    popen = job_registry.get(ticket_id)
    had_running = popen is not None

    if popen is not None:
        if sync:
            _terminate_with_grace(ticket_id, popen)
        else:
            t = threading.Thread(
                target=_terminate_with_grace,
                args=(ticket_id, popen),
                daemon=True,
                name=f"cancel-{ticket_id}",
            )
            t.start()

    if issue_id:
        if had_running:
            body = (
                f"{HEADER_CANCELLED}\n\n"
                f"Run aborted — process group received SIGTERM "
                f"(SIGKILL after {GRACE_BEFORE_KILL_SECONDS}s if still alive). "
                f"{cancelled_jobs} queue job(s) flagged cancelled."
            )
        elif cancelled_jobs > 0:
            body = (
                f"{HEADER_CANCELLED}\n\n"
                f"No active subprocess for this ticket. "
                f"{cancelled_jobs} pending queue job(s) flagged cancelled."
            )
        else:
            body = f"{HEADER_CANCELLED}\n\nNo active run or pending job — nothing to cancel."

        try:
            linear_api.post_comment(issue_id, body)
        except Exception as exc:
            logger.warning("cancel — ticket=%s post_comment failed: %s", ticket_id, exc)

    logger.info(
        "cancel — ticket=%s cancelled_jobs=%d had_running=%s",
        ticket_id, cancelled_jobs, had_running,
    )
    return {"cancelled_jobs": cancelled_jobs, "had_running_process": had_running}
