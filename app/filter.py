"""Decide whether and how to react to a Linear webhook payload.

Two transition groups trigger us:

- ``* → AI Planning & Research`` — start the executor with the planning prompt,
  then set state to ``Human Design Review``.
- ``* → AI Implementation`` — start the executor with the implementation prompt,
  then set state to ``Draft PR Ready``.
- ``* → Done`` (Stage 2) — BASIC approved final completion: merge the work
  branch when one exists, clean up the worktree, post a final comment.
- ``* → AI Review Watch`` — mark the linked draft PR ready for review.

Anything else is ignored (we still respond 200 so Linear doesn't retry).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("linear-executor")


PLANNING_STATE_NAME = "AI Planning & Research"
START_STATE_NAME = "AI Implementation"
START_STATE_NAMES = (PLANNING_STATE_NAME, START_STATE_NAME)
COMPLETE_STATE_NAME = "Done"
DRAFT_READY_STATE_NAME = "Draft PR Ready"
CANCEL_STATE_NAME = "Stop AI"
REVIEW_WATCH_STATE_NAME = "AI Review Watch"

# Tickets in this Linear project run via the lightweight proxy flow
# (Phase 4): no folder mapping, no git worktree, status straight to Done.
# Set LINEAR_PROXY_PROJECT_ID in .env to your own Linear project UUID.
# Empty / unset → proxy flow is disabled (Stage 1 build-with-merge runs on every trigger).
PROXY_PROJECT_ID = os.environ.get("LINEAR_PROXY_PROJECT_ID", "").strip()

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
    """True when the ticket entered any AI-active start state."""
    return any(_is_state_transition_to(payload, state) for state in START_STATE_NAMES)


def should_cancel_run(payload: dict) -> bool:
    """True when the ticket just transitioned into Canceled.

    Cancel is a hard abort signal: any in-flight Claude subprocess for this
    ticket should be terminated, the queue should mark pending/running jobs
    as cancelled, and a comment should be posted. The Linear status stays
    on Canceled (no auto-bounce to Todo) so the user can retrigger
    explicitly via Todo → AI Implementation if desired.
    """
    return _is_state_transition_to(payload, CANCEL_STATE_NAME)


def should_start_review_watch(payload: dict) -> bool:
    """True when the ticket entered the PR review-watch lane."""
    return _is_state_transition_to(payload, REVIEW_WATCH_STATE_NAME)


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
    """True if the ticket lives in the ⚡ Ad-hoc AI Proxy project.

    Linear's webhook payload does NOT include the project field by default
    (verified 2026-04-25 via debug-log of TES-619). The data dict contains
    ``team``/``teamId`` but no ``project``/``projectId``. To detect proxy
    tickets we fall back to a GraphQL lookup when the payload is empty.
    See TES-617 for the full diagnosis.
    """
    if not PROXY_PROJECT_ID:
        return False

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
