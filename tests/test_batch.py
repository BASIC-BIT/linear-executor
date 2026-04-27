"""Tests for AI Batch mode (TES-612).

Batch is the unattended-marathon variant of Stage1: same orchestrator
path, but final state is Done (not In Review). Multiple tickets can be
queued on AI Batch and the worker processes them sequentially.
"""
from __future__ import annotations

import pytest

from app import filter as f
from app import orchestrator
from app.linear_api import Comment


def _payload(identifier="TES-700", issue_id="issue-uuid", state_name="AI Batch"):
    return {
        "action": "update",
        "type": "Issue",
        "data": {
            "id": issue_id,
            "identifier": identifier,
            "title": "A batch ticket",
            "description": "Do the thing.",
            "project": {"name": "Radio MCP"},
            "state": {"name": state_name},
        },
        "updatedFrom": {"stateId": "prev"},
    }


def test_should_start_batch_run_true_on_transition_to_ai_batch():
    payload = _payload(state_name="AI Batch")
    assert f.should_start_batch_run(payload) is True


def test_should_start_batch_run_true_on_create_with_ai_batch():
    payload = {
        "action": "create",
        "data": {"id": "x", "state": {"name": "AI Batch"}},
    }
    assert f.should_start_batch_run(payload) is True


def test_should_start_batch_run_false_for_other_states():
    for name in ("Todo", "AI Implementation", "Done", "In Review", "Stop AI"):
        payload = {
            "action": "update",
            "updatedFrom": {"stateId": "old"},
            "data": {"state": {"name": name}},
        }
        assert f.should_start_batch_run(payload) is False, f"failed for {name}"


@pytest.fixture
def patched_orchestrator(monkeypatch):
    """Replace every external dependency of orchestrator with a controllable stub."""
    state = {
        "fetched_for": [],
        "downloaded_into": [],
        "comments": [],
        "state_changes": [],
    }

    def fake_fetch_attachments(issue_id, client=None):
        return state.get("_attachments_to_return", [])

    def fake_fetch_comments(issue_id, client=None):
        return state.get("_comments_to_return", [])

    def fake_download(att_list, target_dir, api_key=None, client=None):
        return [target_dir / a.title for a in att_list]

    def fake_post(issue_id, body, client=None):
        state["comments"].append({"issue_id": issue_id, "body": body})
        return f"comment-id-{len(state['comments'])}"

    def fake_state_id(team_id, name, client=None):
        return f"state-id-for-{name}"

    def fake_set_state(issue_id, state_id, client=None):
        state["state_changes"].append({"issue_id": issue_id, "state_id": state_id})
        return True

    monkeypatch.setattr(orchestrator.linear_api, "fetch_issue_attachments", fake_fetch_attachments)
    monkeypatch.setattr(orchestrator.linear_api, "fetch_issue_comments", fake_fetch_comments)
    monkeypatch.setattr(orchestrator.attachments_mod, "download_attachments", fake_download)
    monkeypatch.setattr(orchestrator.linear_api, "post_comment", fake_post)
    monkeypatch.setattr(orchestrator.linear_api, "fetch_workflow_state_id", fake_state_id)
    monkeypatch.setattr(orchestrator.linear_api, "set_issue_state", fake_set_state)
    monkeypatch.setattr(orchestrator, "_state_id_cache", {})

    return state


def test_orchestrate_start_default_final_state_is_in_review(patched_orchestrator):
    orchestrator.orchestrate_start(_payload(state_name="AI Implementation"), delivery_id="d-1")
    state_changes = patched_orchestrator["state_changes"]
    assert len(state_changes) == 1
    assert state_changes[0]["state_id"] == "state-id-for-In Review"


def test_orchestrate_start_with_final_state_done_for_batch(patched_orchestrator):
    """Batch jobs go directly to Done — no In-Review-Roundtrip."""
    orchestrator.orchestrate_start(
        _payload(state_name="AI Batch"),
        delivery_id="d-batch",
        final_state="Done",
    )
    state_changes = patched_orchestrator["state_changes"]
    assert len(state_changes) == 1
    assert state_changes[0]["state_id"] == "state-id-for-Done"


def test_worker_dispatches_batch_kind_to_orchestrate_start_with_done(monkeypatch):
    """The worker should call orchestrate_start with final_state='Done' for kind=batch."""
    from app import worker as worker_mod
    captured = {}
    def fake_orch_start(payload, delivery_id=None, *, final_state="In Review"):
        captured["final_state"] = final_state
        captured["delivery_id"] = delivery_id
    monkeypatch.setattr(worker_mod, "orchestrate_start", fake_orch_start)

    job = type("J", (), {
        "kind": "batch",
        "payload": _payload(state_name="AI Batch"),
        "delivery_id": "d-batch-w",
    })()
    worker_mod._dispatch(job)

    assert captured["final_state"] == "Done"
    assert captured["delivery_id"] == "d-batch-w"
