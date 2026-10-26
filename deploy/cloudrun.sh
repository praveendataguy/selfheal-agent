#!/usr/bin/env bash
# One-time setup + deploy of the self-heal agent to Cloud Run.
# Usage: PROJECT_ID=my-proj REGION=us-central1 ./deploy/cloudrun.sh
# Secrets are read from your local .env the first time and stored in Secret Manager.
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-selfheal-agent}"
SA="${SERVICE}@${PROJECT_ID}.iam.gserviceaccount.com"
REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/${SERVICE}"
IMAGE="${REPO}/agent:${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo latest)}"
SECRETS=(GITLAB_TOKEN GITLAB_WEBHOOK_SECRET ANTHROPIC_API_KEY SLACK_BOT_TOKEN SLACK_SIGNING_SECRET)

gcloud config set project "$PROJECT_ID" >/dev/null
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  secretmanager.googleapis.com logging.googleapis.com monitoring.googleapis.com aiplatform.googleapis.com

# --- service account with least privilege
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SERVICE" --display-name "Self-heal CI/CD agent"
for role in roles/logging.logWriter roles/logging.viewer roles/monitoring.metricWriter roles/aiplatform.user; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$SA" --role "$role" \
    --condition=None >/dev/null
done

# --- secrets (created from .env on first run; later rotate with `gcloud secrets versions add`)
if [[ -f .env ]]; then set -a; source .env; set +a; fi
SECRET_FLAGS=()
for name in "${SECRETS[@]}"; do
  if ! gcloud secrets describe "$name" >/dev/null 2>&1; then
    value="${!name:-}"
    if [[ -z "$value" ]]; then echo "skip secret $name (empty)"; continue; fi
    printf '%s' "$value" | gcloud secrets create "$name" --data-file=- --replication-policy=automatic
  fi
  gcloud secrets add-iam-policy-binding "$name" --member "serviceAccount:$SA" \
    --role roles/secretmanager.secretAccessor >/dev/null
  SECRET_FLAGS+=("${name}=${name}:latest")
done

# --- build
gcloud artifacts repositories describe "$SERVICE" --location "$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "$SERVICE" --repository-format docker --location "$REGION"
gcloud builds submit --tag "$IMAGE" .

# --- deploy
# --no-cpu-throttling keeps CPU allocated after the HTTP response, so the background
# analysis started by a webhook can finish. Concurrency is modest: each analysis is LLM-bound.
IFS=,; SECRET_ARG="${SECRET_FLAGS[*]}"; unset IFS
gcloud run deploy "$SERVICE" \
  --image "$IMAGE" \
  --region "$REGION" \
  --service-account "$SA" \
  --allow-unauthenticated \
  --no-cpu-throttling \
  --min-instances 0 --max-instances 5 --concurrency 10 \
  --cpu 1 --memory 1Gi --timeout 600 \
  --set-secrets "$SECRET_ARG" \
  --set-env-vars "^@^LLM_PROVIDER=${LLM_PROVIDER:-anthropic}@CLAUDE_MODEL=${CLAUDE_MODEL:-claude-sonnet-5-5}\
@VERTEX_PROJECT_ID=${PROJECT_ID}@VERTEX_REGION=${VERTEX_REGION:-us-east5}@GCP_PROJECT_ID=${PROJECT_ID}\
@GITLAB_URL=${GITLAB_URL:-https://gitlab.com}@SLACK_CHANNEL_ID=${SLACK_CHANNEL_ID:-}\
@SLACK_APPROVER_IDS=${SLACK_APPROVER_IDS:-}@PRODUCTION_REFS=${PRODUCTION_REFS:-main,master,production,release/*}\
@DRY_RUN=${DRY_RUN:-true}@FETCH_CLOUD_LOGGING_CONTEXT=${FETCH_CLOUD_LOGGING_CONTEXT:-false}"

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')
cat <<EOF

Deployed: $URL
  GitLab webhook URL (Settings > Webhooks, trigger: Pipeline events): $URL/webhooks/gitlab
  Slack interactivity Request URL:                                  $URL/slack/interactions
EOF
