import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from app import main
from app.approval.slack import APPROVE
from app.config import get_settings


class RecordingOrchestrator:
    def __init__(self):
        self.events, self.decisions = [], []
        self.notifier = None

    def handle_pipeline_event(self, payload):
        self.events.append(payload)
        return "ok"

    def handle_decision(self, approved, project_id, mr_iid, branch, user):
        self.decisions.append((approved, project_id, mr_iid, branch, user))
        return "done"


@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    orch = RecordingOrchestrator()
    main.app.state.orchestrator = orch
    yield TestClient(main.app), orch, settings
    main.app.state.orchestrator = None
    get_settings.cache_clear()


def hook(status="failed", ref="main"):
    return {"object_kind": "pipeline", "object_attributes": {"id": 1, "status": status, "ref": ref}, "project": {"id": 2}}


def test_webhook_rejects_bad_token(client):
    c, _, _ = client
    r = c.post("/webhooks/gitlab", json=hook(), headers={"X-Gitlab-Token": "nope", "X-Gitlab-Event": "Pipeline Hook"})
    assert r.status_code == 401


def test_webhook_accepts_failed_pipeline(client):
    c, orch, _ = client
    r = c.post("/webhooks/gitlab", json=hook(), headers={"X-Gitlab-Token": "hook-secret", "X-Gitlab-Event": "Pipeline Hook"})
    assert r.json() == {"status": "accepted"}
    assert len(orch.events) == 1  # background task ran


def test_webhook_ignores_running_pipeline(client):
    c, orch, _ = client
    r = c.post("/webhooks/gitlab", json=hook("running"), headers={"X-Gitlab-Token": "hook-secret", "X-Gitlab-Event": "Pipeline Hook"})
    assert r.json()["status"].startswith("ignored") and orch.events == []


def slack_post(c, secret, payload):
    body = urlencode({"payload": json.dumps(payload)})
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return c.post(
        "/slack/interactions",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
        },
    )


def slack_payload(user_id="U1"):
    return {
        "type": "block_actions",
        "user": {"id": user_id, "username": "alice"},
        "channel": {"id": "C1"},
        "message": {"ts": "123.456"},
        "actions": [{"action_id": APPROVE, "value": json.dumps({"p": 2, "mr": 7, "b": "selfheal/main-1"})}],
    }


def test_slack_approval_signed(client):
    c, orch, settings = client
    r = slack_post(c, settings.slack_signing_secret, slack_payload())
    assert r.status_code == 200
    assert orch.decisions == [(True, 2, 7, "selfheal/main-1", "alice")]


def test_slack_bad_signature(client):
    c, orch, _ = client
    assert slack_post(c, "wrong-secret", slack_payload()).status_code == 401
    assert orch.decisions == []


def test_slack_unauthorized_approver(client):
    c, orch, settings = client
    settings.slack_approver_ids = "U999"
    r = slack_post(c, settings.slack_signing_secret, slack_payload("U1"))
    assert "not an approver" in r.json()["text"] and orch.decisions == []
