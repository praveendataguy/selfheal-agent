"""Structured logging for Google Cloud Logging, plus optional runtime-error context.

On Cloud Run, anything written to stdout as one JSON object per line is parsed by
Cloud Logging: `severity` and `message` are recognised, other keys land in jsonPayload.
Each agent milestone is logged with an `event` field so log-based metrics and alerting
policies can count them (see deploy/observability.sh).
"""

import json
import logging
import sys
from datetime import datetime, timedelta, timezone

RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message"}


class CloudJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "time": datetime.now(timezone.utc).isoformat(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in RESERVED})
        if record.exc_info:
            payload["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(CloudJsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def event(name: str, **fields) -> None:
    """Emit a countable agent event, e.g. event("fix.mr_opened", project="g/p")."""
    logging.getLogger("selfheal.events").info(name, extra={"event": name, **fields})


def recent_runtime_errors(gcp_project: str, service_hint: str, minutes: int = 30, limit: int = 30) -> str:
    """Pull recent ERROR entries from Cloud Logging to help diagnose deployment drift / runtime issues.

    Needs `google-cloud-logging` and a service account with roles/logging.viewer.
    Returns an empty string if unavailable so the agent degrades gracefully.
    """
    try:
        from google.cloud import logging as gcl  # type: ignore
    except ImportError:
        return ""
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    flt = f'severity>=ERROR AND timestamp>="{since}" AND "{service_hint}"'
    try:
        client = gcl.Client(project=gcp_project)
        lines = []
        for entry in client.list_entries(filter_=flt, order_by=gcl.DESCENDING, max_results=limit):
            text = entry.payload if isinstance(entry.payload, str) else json.dumps(entry.payload, default=str)
            lines.append(f"{entry.timestamp} {entry.severity} {text[:500]}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 - observability context is best-effort
        logging.getLogger(__name__).warning("cloud logging fetch failed", exc_info=True)
        return ""
