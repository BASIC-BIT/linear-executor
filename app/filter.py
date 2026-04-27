"""Decide whether and how to react to a Linear webhook payload.

Two transitions trigger us:

- ``* → AI Implementation`` (Stage 1) — start the executor: resolve folder,
  download attachments, run Claude Code, post results, set state to ``In Review``.
- ``In Review → Done`` (Stage 2) — Bastian approved: merge the work branch
  (when one exists), clean up the worktree, post a final comment.

Anything else is ignored (we still respond 200 so Linear doesn't retry).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("linear-executor")


START_STATE_NAME = "AI Implementation"
COMPLETE_STATE_NAME = "Done"
REVIEW_STATE_NAME = "In Review"
CANCEL_STATE_NAME = "Stop AI"
BATCH_STATE_NAME = "AI Batch"

# Tickets in this Linear project run via the lightweight proxy flow
# (Phase 4): no folder mapping, no git worktree, status straight to Done.
PROXY_PROJECT_ID = "02bda033-9e62-4ee4-a90a-fdc9c0858032"

# Backwards-compatible alias used by older imports / tests.
TRIGGER_STATE_NAME = START_STATE_NAME


def _is_state_transition_to(payload: dict, state_name: str) -> bool:
    """True if this webhook represents a transition into ``state_name``.

    Two shapes count as such a transition:

    1. ``action="update"`` with a ``state`` matching ``state_name`` AND
       an ``updatedFrom.stateId`` (Linear's standard status-change event).
    2. ``action="create"`` with the ticket already in ``state_name`` (e.g.
       a ticket created via API directly into ``AI Implementation``).
       This unblocks the mobile-style flow where Bastian pre-sets the
       trigger state instead of bouncing the status afterwards.
    """
    data = payload.get("data") or {}
    state = data.get("state") or {}
    if state.get("name") != state_name:
        return False

    action = payload.get("action")
    if action == "create":
        return True
    if action == "update":
        updated_from = payload.get("updatedFrom") or {}
        return "stateId" in updated_from
    return False


def should_start_execution(payload: dict) -> bool:
    """True when the ticket either just transitioned into AI Implementation
    or was just created already in that state."""
    return _is_state_transition_to(payload, START_STATE_NAME)


def should_start_batch_run(payload: dict) -> bool:
    """True when the ticket transitioned into AI Batch.

    Batch mode is for marathon scenarios where the user wants several
    tickets worked through unattended without the In-Review-Roundtrip.
    Each batch ticket runs through the same orchestrator path as a
    Stage1 run, but the final state goes directly to Done instead of
    In Review. The user can queue many tickets on AI Batch; the worker
    processes them sequentially (one Claude subprocess at a time per
    lane).
    """
    return _is_state_transition_to(payload, BATCH_STATE_NAME)


def should_cancel_run(payload: dict) -> bool:
    """True when the ticket just transitioned into Canceled.

    Cancel is a hard abort signal: any in-flight Claude subprocess for this
    ticket should be terminated, the queue should mark pending/running jobs
    as cancelled, and a comment should be posted. The Linear status stays
    on Canceled (no auto-bounce to Todo) so the user can retrigger
    explicitly via Todo → AI Implementation if desired.
    """
    return _is_state_transition_to(payload, CANCEL_STATE_NAME)


def should_complete_review(payload: dict) -> bool:
    """True when the ticket just transitioned into Done.

    Proxy-project tickets are excluded: their Done transition is set by the
    executor itself after the proxy run, and re-firing Stage 2 would just
    spam a redundant 'no worktree to merge' comment.
    """
    if not _is_state_transition_to(payload, COMPLETE_STATE_NAME):
        return False
    if is_proxy_ticket(payload):
        return False
    return True


def is_proxy_ticket(payload: dict) -> bool:
    """True if the ticket lives in the ⚡ Ad-hoc Proxy project.

    Linear's webhook payload does NOT include the project field by default
    (verified 2026-04-25 via debug-log of TES-619). The data dict contains
    ``team``/``teamId`` but no ``project``/``projectId``. To detect proxy
    tickets we fall back to a GraphQL lookup when the payload is empty.
    See TES-617 for the full diagnosis.
    """
    data = payload.get("data") or {}
    project = data.get("project") or {}
    project_id = project.get("id")

    if project_id is not None:
        return project_id == PROXY_PROJECT_ID

    # Fallback: payload missing project info, fetch from Linear API
    issue_id = data.get("id")
    if not issue_id:
        return False
    try:
        from app import linear_api
        fetched = linear_api.fetch_issue_project_id(issue_id)
    except Exception as exc:
        logger.warning(
            "is_proxy_ticket — could not fetch project for %s: %s", issue_id, exc
        )
        return False
    return fetched == PROXY_PROJECT_ID


def should_trigger(payload: dict) -> bool:
    """Backwards-compatible alias for the original Stage-1 trigger check."""
    return should_start_execution(payload)
