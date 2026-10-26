"""HTTP entrypoints: GitLab pipeline webhooks and Slack interactive approvals."""

import hmac
import json
import logging
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from app.approval import NullNotifier
from app.approval.slack import APPROVE, REJECT, SlackNotifier, verify_slack_request
from app.config import Settings, get_settings
from app.gitlab_client import GitLabClient
from app.observability import event, setup_logging
from app.orchestrator import Orchestrator

setup_logging()
log = logging.getLogger("selfheal.api")
app = FastAPI(title="Self-Healing CI/CD Agent", version="0.1.0")


def build_orchestrator(settings: Settings) -> Orchestrator:
    notifier = (
        SlackNotifier(settings.slack_bot_token, settings.slack_channel_id)
        if settings.slack_bot_token and settings.slack_channel_id
        else NullNotifier()
    )
    return Orchestrator(settings, GitLabClient(settings.gitlab_url, settings.gitlab_token), notifier)


def orchestrator() -> Orchestrator:
    if getattr(app.state, "orchestrator", None) is None:
        app.state.orchestrator = build_orchestrator(get_settings())
    return app.state.orchestrator


def _safe(fn, *args, **kwargs) -> None:
    """Background work must never crash silently: log with context instead."""
    try:
        result = fn(*args, **kwargs)
        log.info("task finished", extra={"result": result})
    except Exception:
        log.exception("task failed", extra={"event": "agent.error"})


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/webhooks/gitlab")
async def gitlab_webhook(
    request: Request,
    background: BackgroundTasks,
    x_gitlab_token: str = Header(default=""),
    x_gitlab_event: str = Header(default=""),
):
    settings = get_settings()
    if not settings.gitlab_webhook_secret or not hmac.compare_digest(
        x_gitlab_token.encode(), settings.gitlab_webhook_secret.encode()
    ):
        raise HTTPException(status_code=401, detail="invalid webhook token")
    if x_gitlab_event == "Merge Request Hook":
        payload = await request.json()
        background.add_task(_safe, orchestrator().handle_merge_request_event, payload)
        return {"status": "accepted"}
    if x_gitlab_event != "Pipeline Hook":
        return {"status": f"ignored: {x_gitlab_event or 'unknown'} event"}

    payload = await request.json()
    status = payload.get("object_attributes", {}).get("status")
    ref = payload.get("object_attributes", {}).get("ref", "")
    duo_success = settings.healer == "duo" and status == "success"
    if status == "failed" or duo_success or (status == "success" and ref.startswith(settings.branch_prefix)):
        # GitLab expects a fast response; analysis can take a minute.
        background.add_task(_safe, orchestrator().handle_pipeline_event, payload)
        return {"status": "accepted"}
    return {"status": f"ignored: status={status}"}


@app.post("/slack/interactions")
async def slack_interactions(request: Request, background: BackgroundTasks):
    settings = get_settings()
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not verify_slack_request(settings.slack_signing_secret, body, headers):
        raise HTTPException(status_code=401, detail="invalid slack signature")

    payload = json.loads(parse_qs(body.decode())["payload"][0])
    action = (payload.get("actions") or [{}])[0]
    if action.get("action_id") not in (APPROVE, REJECT):
        return {}

    user = payload.get("user", {})
    user_id, user_name = user.get("id", ""), user.get("username") or user.get("name", "unknown")
    if settings.approver_ids and user_id not in settings.approver_ids:
        event("approval.unauthorized", user=user_id)
        return {"response_type": "ephemeral", "replace_original": False, "text": "You're not an approver for self-heal patches."}

    value = json.loads(action["value"])
    approved = action["action_id"] == APPROVE
    channel = payload.get("channel", {}).get("id")
    ts = payload.get("message", {}).get("ts") or payload.get("container", {}).get("message_ts")
    background.add_task(
        _decide, approved, int(value["p"]), int(value["mr"]), value["b"], user_name, channel, ts
    )
    return {}


def _decide(approved: bool, project_id: int, mr_iid: int, branch: str, user: str, channel: str, ts: str) -> None:
    orch = orchestrator()
    try:
        outcome = orch.handle_decision(approved, project_id, mr_iid, branch, user)
    except Exception as exc:
        log.exception("decision failed", extra={"event": "agent.error"})
        outcome = f":warning: Could not apply decision: {exc}"
    if isinstance(orch.notifier, SlackNotifier) and channel and ts:
        icon = ":white_check_mark:" if approved else ":no_entry_sign:"
        orch.notifier.resolve_message(channel, ts, f"{icon} {outcome}")
