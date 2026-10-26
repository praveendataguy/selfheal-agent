"""Slack human-in-the-loop approvals using Block Kit buttons.

Slack app setup (api.slack.com/apps):
  * Bot token scopes: chat:write
  * Interactivity: Request URL = https://<service>/slack/interactions
  * Invite the bot to SLACK_CHANNEL_ID
"""

import json
import time

from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier

from app.approval import ApprovalRequest

APPROVE = "selfheal_approve"
REJECT = "selfheal_reject"


def approval_blocks(req: ApprovalRequest) -> list[dict]:
    value = json.dumps({"p": req.project_id, "mr": req.mr_iid, "b": req.branch})
    reasons = "\n".join(f"• {r}" for r in req.reasons) or "• policy"
    files = ", ".join(f"`{f}`" for f in req.files[:8])
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": ":rotating_light: Self-heal patch needs approval"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Project*\n{req.project_path}"},
                {"type": "mrkdwn", "text": f"*Target*\n`{req.target_ref}`"},
                {"type": "mrkdwn", "text": f"*Category*\n{req.category}"},
                {"type": "mrkdwn", "text": f"*Confidence / risk*\n{req.confidence:.0%} / {req.risk}"},
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Root cause*\n{req.root_cause[:2500]}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Why approval is required*\n{reasons}"}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Files*: {files}\n<{req.mr_url}|Review merge request !{req.mr_iid}>"},
        },
        {
            "type": "actions",
            "block_id": "selfheal_decision",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve & run pipeline"},
                    "action_id": APPROVE,
                    "value": value,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Approve patch?"},
                        "text": {"type": "plain_text", "text": "Marks the MR ready and runs its pipeline."},
                        "confirm": {"type": "plain_text", "text": "Approve"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "action_id": REJECT,
                    "value": value,
                },
            ],
        },
    ]


class SlackNotifier:
    def __init__(self, bot_token: str, channel_id: str, client: WebClient | None = None):
        self.client = client or WebClient(token=bot_token)
        self.channel = channel_id

    def request_approval(self, req: ApprovalRequest) -> None:
        self.client.chat_postMessage(
            channel=self.channel,
            text=f"Self-heal patch for {req.project_path} needs approval: {req.mr_url}",
            blocks=approval_blocks(req),
        )

    def notify(self, text: str) -> None:
        self.client.chat_postMessage(channel=self.channel, text=text)

    def resolve_message(self, channel: str, ts: str, text: str) -> None:
        """Replace the buttons with the outcome so nobody can click twice."""
        self.client.chat_update(
            channel=channel, ts=ts, text=text, blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
        )


def verify_slack_request(signing_secret: str, body: bytes, headers: dict) -> bool:
    ts = headers.get("x-slack-request-timestamp", "0")
    try:
        if abs(time.time() - int(ts)) > 300:  # replay protection
            return False
    except ValueError:
        return False
    return SignatureVerifier(signing_secret).is_valid(
        body=body, timestamp=ts, signature=headers.get("x-slack-signature", "")
    )
