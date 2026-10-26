"""Send a fake 'pipeline failed' webhook to a running agent, for local testing.

    python scripts/replay_webhook.py --project-id 123 --pipeline-id 456 --ref feature/x
    python scripts/replay_webhook.py --url https://<cloud-run-url> ...

The project and pipeline must exist in GitLab: the agent fetches the real logs.
"""

import argparse
import os

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://localhost:8080")
parser.add_argument("--project-id", type=int, required=True)
parser.add_argument("--pipeline-id", type=int, required=True)
parser.add_argument("--ref", default="main")
parser.add_argument("--status", default="failed")
args = parser.parse_args()

payload = {
    "object_kind": "pipeline",
    "object_attributes": {"id": args.pipeline_id, "status": args.status, "ref": args.ref},
    "project": {"id": args.project_id},
    "commit": {"message": ""},
}
resp = httpx.post(
    f"{args.url.rstrip('/')}/webhooks/gitlab",
    json=payload,
    headers={"X-Gitlab-Event": "Pipeline Hook", "X-Gitlab-Token": os.environ.get("GITLAB_WEBHOOK_SECRET", "")},
    timeout=30,
)
print(resp.status_code, resp.text)
