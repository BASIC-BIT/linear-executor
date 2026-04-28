import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import queue as q
from app.main import create_app


SECRET = "lin_wh_secret_test_value"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("LINEAR_EXECUTOR_DB", str(tmp_path / "jobs.db"))
    app = create_app()
    return TestClient(app), tmp_path / "jobs.db"


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _payload() -> dict:
    return {
        "action": "update",
        "type": "Issue",
        "data": {
            "id": "b245856c-3713-482c-9e82-0ec57aa0db41",
            "identifier": "TES-458",
            "title": "Konzept: Claude Code Executor",
            "state": {"name": "Do It"},
        },
        "webhookTimestamp": 1713398400000,
        "webhookId": "00000000-0000-0000-0000-000000000000",
    }


def test_health_returns_200(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_webhook_valid_signature_returns_200(client):
    c, _ = client
    body = json.dumps(_payload()).encode()
    sig = _sign(body)
    r = c.post("/webhook", content=body, headers={"linear-signature": sig})
    assert r.status_code == 200


def test_webhook_ignores_non_trigger_payload(client):
    c, db_path = client
    payload = _payload()
    payload["data"]["state"] = {"name": "Backlog"}
    body = json.dumps(payload).encode()
    sig = _sign(body)
    r = c.post("/webhook", content=body, headers={"linear-signature": sig})
    assert r.status_code == 200
    assert r.json()["stage"] == "ignored"
    assert r.json()["job_id"] is None


def test_webhook_enqueues_start_job_on_ai_implementation_transition(client):
    c, db_path = client
    payload = _payload()
    payload["data"]["state"] = {"name": "AI Implementation", "type": "started"}
    payload["updatedFrom"] = {"stateId": "previous-state-id"}
    body = json.dumps(payload).encode()
    sig = _sign(body)
    r = c.post("/webhook", content=body, headers={"linear-signature": sig})
    assert r.status_code == 200
    data = r.json()
    assert data["stage"] == "stage1"
    assert data["job_id"] is not None
    job = q.get_job(db_path, data["job_id"])
    assert job.kind == "start"
    assert job.status == "pending"
    assert job.identifier == "TES-458"


def test_webhook_enqueues_complete_job_on_done_transition(client):
    c, db_path = client
    payload = _payload()
    payload["data"]["state"] = {"name": "Done", "type": "completed"}
    payload["updatedFrom"] = {"stateId": "previous-state-id"}
    body = json.dumps(payload).encode()
    sig = _sign(body)
    r = c.post("/webhook", content=body, headers={"linear-signature": sig})
    assert r.status_code == 200
    data = r.json()
    assert data["stage"] == "stage2"
    job = q.get_job(db_path, data["job_id"])
    assert job.kind == "complete"


def test_webhook_enqueues_proxy_job_for_proxy_project(client):
    c, db_path = client
    from app.filter import PROXY_PROJECT_ID
    payload = _payload()
    payload["data"]["state"] = {"name": "AI Implementation", "type": "started"}
    payload["data"]["project"] = {"id": PROXY_PROJECT_ID, "name": "⚡ Ad-hoc AI Proxy"}
    payload["updatedFrom"] = {"stateId": "previous-state-id"}
    body = json.dumps(payload).encode()
    sig = _sign(body)
    r = c.post("/webhook", content=body, headers={"linear-signature": sig})
    assert r.status_code == 200
    data = r.json()
    assert data["stage"] == "proxy"
    job = q.get_job(db_path, data["job_id"])
    assert job.kind == "proxy"


def test_webhook_invalid_signature_returns_401(client):
    c, _ = client
    body = json.dumps(_payload()).encode()
    r = c.post("/webhook", content=body, headers={"linear-signature": "deadbeef" * 8})
    assert r.status_code == 401


def test_webhook_missing_signature_returns_401(client):
    c, _ = client
    body = json.dumps(_payload()).encode()
    r = c.post("/webhook", content=body)
    assert r.status_code == 401


def test_webhook_valid_signature_but_broken_json_returns_400(client):
    c, _ = client
    body = b"{not valid json"
    sig = _sign(body)
    r = c.post("/webhook", content=body, headers={"linear-signature": sig})
    assert r.status_code == 400


def test_webhook_case_insensitive_signature_header(client):
    c, _ = client
    body = json.dumps(_payload()).encode()
    sig = _sign(body)
    # FastAPI/Starlette behandelt Header case-insensitive; Linear sendet "Linear-Signature"
    r = c.post("/webhook", content=body, headers={"Linear-Signature": sig})
    assert r.status_code == 200
