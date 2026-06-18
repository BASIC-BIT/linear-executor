from app.filter import (
    PROXY_PROJECT_ID,
    is_proxy_ticket,
    should_complete_review,
    should_start_review_watch,
    should_start_execution,
    should_trigger,
)


def _base_payload(**overrides) -> dict:
    payload = {
        "action": "update",
        "type": "Issue",
        "data": {
            "identifier": "TES-452",
            "state": {"id": "6e17f5d0", "name": "AI Implementation", "type": "started"},
        },
        "updatedFrom": {"stateId": "f5753ce3"},
    }
    payload.update(overrides)
    return payload


def test_triggers_on_fresh_transition_to_ai_implementation():
    assert should_trigger(_base_payload()) is True


def test_triggers_on_fresh_transition_to_ai_planning_research():
    p = _base_payload()
    p["data"]["state"] = {"name": "AI Planning & Research", "type": "started"}
    assert should_trigger(p) is True


def test_does_not_trigger_when_state_unchanged():
    # Description edit while already in AI Implementation — updatedFrom has no stateId
    p = _base_payload(updatedFrom={"description": "old description text"})
    assert should_trigger(p) is False


def test_does_not_trigger_for_other_state():
    p = _base_payload()
    p["data"]["state"] = {"name": "In Progress", "type": "started"}
    assert should_trigger(p) is False


def test_triggers_on_create_with_target_state():
    """Mobile flow: ticket created via API directly into AI Implementation
    must fire the executor — otherwise users have to bounce the status
    manually after creating it. (TES-610)"""
    p = _base_payload(action="create")
    p.pop("updatedFrom", None)
    assert should_trigger(p) is True


def test_does_not_trigger_on_create_with_other_state():
    """Tickets created in Backlog/Todo/etc. must not auto-run."""
    p = _base_payload(action="create")
    p.pop("updatedFrom", None)
    p["data"]["state"] = {"name": "Backlog", "type": "backlog"}
    assert should_trigger(p) is False


def test_does_not_trigger_on_remove_action():
    p = _base_payload(action="remove")
    assert should_trigger(p) is False


def test_does_not_trigger_when_updated_from_missing():
    p = _base_payload()
    del p["updatedFrom"]
    assert should_trigger(p) is False


def test_does_not_trigger_when_state_missing():
    p = _base_payload()
    del p["data"]["state"]
    assert should_trigger(p) is False


def test_triggers_regardless_of_previous_state_value():
    # Whether the previous stateId was "Backlog" or "In Progress" — both should trigger,
    # as long as the move landed in AI Implementation.
    p = _base_payload(updatedFrom={"stateId": "some-other-previous-id"})
    assert should_trigger(p) is True


# --- Phase 4: proxy-ticket detection -------------------------------------------


def _proxy_payload(state="AI Implementation", project_id=PROXY_PROJECT_ID, with_state_transition=True):
    p = {
        "action": "update",
        "type": "Issue",
        "data": {
            "identifier": "TES-PROXY",
            "state": {"name": state, "type": "started"},
            "project": {"id": project_id, "name": "⚡ Ad-hoc AI Proxy"},
        },
    }
    if with_state_transition:
        p["updatedFrom"] = {"stateId": "old"}
    return p


def test_is_proxy_ticket_true_for_proxy_project():
    assert is_proxy_ticket(_proxy_payload()) is True


def test_is_proxy_ticket_false_for_other_project():
    p = _proxy_payload(project_id="some-other-uuid")
    assert is_proxy_ticket(p) is False


def test_is_proxy_ticket_false_when_no_project():
    p = _proxy_payload()
    p["data"].pop("project")
    assert is_proxy_ticket(p) is False


def test_should_start_execution_still_true_for_proxy_tickets():
    """Phase 4: proxy tickets use the same trigger event as regular ones —
    the dispatch decision (proxy vs. regular) happens in main.py based on
    is_proxy_ticket(), not in the filter itself."""
    assert should_start_execution(_proxy_payload()) is True


def test_should_complete_review_skips_proxy_done_to_avoid_loop():
    """When the executor itself sets a proxy ticket to Done, Linear sends a
    webhook back. Stage 2 must not fire on it — otherwise the user gets a
    redundant 'no worktree to merge' comment."""
    p = _proxy_payload(state="Done")
    assert should_complete_review(p) is False


def test_should_complete_review_still_fires_for_non_proxy_done():
    p = _base_payload()
    p["data"]["state"] = {"name": "Done"}
    # base payload has no project field — non-proxy by default
    assert should_complete_review(p) is True


def test_should_start_review_watch_triggers_on_ai_review_watch_transition():
    p = _base_payload()
    p["data"]["state"] = {"name": "AI Review Watch", "type": "started"}
    assert should_start_review_watch(p) is True


def test_should_start_review_watch_ignores_description_edits():
    p = _base_payload(updatedFrom={"description": "old"})
    p["data"]["state"] = {"name": "AI Review Watch", "type": "started"}
    assert should_start_review_watch(p) is False
