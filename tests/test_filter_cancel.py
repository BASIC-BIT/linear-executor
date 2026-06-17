"""Filter-level tests for the cancel trigger (TES-596)."""
from __future__ import annotations

from app import filter as f


def test_should_cancel_run_true_on_state_transition_to_canceled():
    payload = {
        "action": "update",
        "updatedFrom": {"stateId": "old-state"},
        "data": {"id": "iss-1", "state": {"name": "Stop AI"}},
    }
    assert f.should_cancel_run(payload) is True


def test_should_cancel_run_true_when_created_directly_in_canceled():
    """User creates a ticket already in Canceled state — rare but valid."""
    payload = {
        "action": "create",
        "data": {"id": "iss-1", "state": {"name": "Stop AI"}},
    }
    assert f.should_cancel_run(payload) is True


def test_should_cancel_run_false_for_other_states():
    for name in ("Todo", "In Progress", "AI Implementation", "Done", "Draft PR Ready"):
        payload = {
            "action": "update",
            "updatedFrom": {"stateId": "old"},
            "data": {"state": {"name": name}},
        }
        assert f.should_cancel_run(payload) is False


def test_should_cancel_run_false_on_non_state_update():
    """Update without state-id change shouldn't be treated as a transition."""
    payload = {
        "action": "update",
        "data": {"state": {"name": "Stop AI"}},
        # no updatedFrom.stateId → not a state transition
    }
    assert f.should_cancel_run(payload) is False
