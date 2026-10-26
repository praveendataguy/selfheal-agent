# Setup and technical details

Back to the [README](../README.md).

## Setup

### 1. GitLab Duo flow
1. Group → **Settings → GitLab Duo**: tick **Allow flow execution**, **Allow foundational flows** and **Fix CI/CD Pipeline** (and **Code Review**).
2. Project → **AI → Flows → Fix CI/CD Pipeline → Enable**.
3. Project → **AI → Triggers → New trigger**: flow **Fix CI/CD Pipeline**, condition **Pipeline events → Failed**.

### 2. Governance agent
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                     # 47 tests
cp .env.example .env          # GITLAB_TOKEN, GITLAB_WEBHOOK_SECRET, Slack values, HEALER=duo
uvicorn app.main:app --port 8080
cloudflared tunnel --url http://localhost:8080   # or deploy (below)
```
- GitLab token: personal or project access token with `api` and `write_repository`.
- Project → **Settings → Webhooks**: URL `https://<agent>/webhooks/gitlab`, secret = `GITLAB_WEBHOOK_SECRET`, triggers **Pipeline events** and **Merge request events**.

### 3. Slack approvals
1. Create an app at api.slack.com/apps with the bot scope `chat:write`, and install it.
2. **Interactivity** Request URL: `https://<agent>/slack/interactions`.
3. Set `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` and `SLACK_CHANNEL_ID`, and invite the bot to the channel. Optionally restrict approvers with `SLACK_APPROVER_IDS`.

## Standalone mode (`HEALER=agent`)

The service can also diagnose failures itself, without Duo. It parses the logs, runs a tool-use loop with an LLM (Claude via the Anthropic API or Vertex AI, Gemini, or a free `mock`), and pushes its own `selfheal/…` fix branch. That mode adds:
- one retry for flaky runner and network failures (no LLM call)
- up to `MAX_ATTEMPTS` repair attempts, then escalation
- stateless incident tracking in commit trailers (`Self-Heal-Attempt`, `Self-Heal-Target`)

## Project layout

```
app/
  main.py            FastAPI: /webhooks/gitlab, /slack/interactions, /healthz
  orchestrator.py    governance of Duo fix MRs; standalone healing loop; approvals
  policy.py          guardrails: blockers and approval triggers
  approval/slack.py  Block Kit approval card + signature verification
  gitlab_client.py   the small slice of the GitLab API the agent uses
  llm.py             standalone-mode diagnosers (Claude, Gemini, mock)
  log_parser.py      log cleanup, error windowing, failure classification
  observability.py   Cloud Logging JSON events
deploy/              Cloud Run deploy script + log-based metrics and alerts
examples/demo-repo   the demo project with the planted bug
scripts/             local diagnosis try-out, webhook replay, model listing
tests/               47 tests with fakes for GitLab, the LLM and Slack
```

## Deploy to Google Cloud Run (optional)

```bash
PROJECT_ID=my-proj REGION=us-central1 ./deploy/cloudrun.sh
PROJECT_ID=my-proj NOTIFICATION_CHANNEL=projects/my-proj/notificationChannels/123 ./deploy/observability.sh
```

## Guardrails in detail (`app/policy.py`)

| Duo's fix… | Decision |
|---|---|
| touches `*.pem`, `*.key`, `.git/` or similar | **Blocked**: labelled `self-heal::blocked`, with a comment and a Slack alert |
| changes too many files or is too large | **Blocked** |
| targets `main`, `master`, `production` or `release/*` | **Human approval** in Slack; the MR is marked Draft until approved |
| touches `.gitlab-ci.yml`, `Dockerfile`, Terraform, `deploy/`, `k8s/`, `helm/` | **Human approval** |
| adds skip or xfail markers, removes assertions, or adds `allow_failure: true` | **Human approval** |
| none of the above | **Auto-verified** |
