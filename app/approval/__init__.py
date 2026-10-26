from dataclasses import dataclass
from typing import Protocol


@dataclass
class ApprovalRequest:
    project_id: int
    project_path: str
    mr_iid: int
    mr_url: str
    branch: str
    target_ref: str
    pipeline_id: int
    root_cause: str
    category: str
    confidence: float
    risk: str
    reasons: list[str]
    files: list[str]


class Notifier(Protocol):
    def request_approval(self, req: ApprovalRequest) -> None: ...

    def notify(self, text: str) -> None: ...


class NullNotifier:
    """Used when Slack isn't configured: logs instead of posting."""

    def __init__(self):
        import logging

        self.log = logging.getLogger("selfheal.notifier")
        self.sent: list = []

    def request_approval(self, req: ApprovalRequest) -> None:
        self.sent.append(req)
        self.log.warning("approval needed but no notifier configured", extra={"mr_url": req.mr_url})

    def notify(self, text: str) -> None:
        self.sent.append(text)
        self.log.info(text)
