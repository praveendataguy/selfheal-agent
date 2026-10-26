#!/usr/bin/env bash
# Log-based metrics + alerting for the agent in Google Cloud Operations Suite.
# Every agent milestone is a JSON log line with an `event` field (see app/observability.py),
# so each metric below is just a filter on that field.
# Usage: PROJECT_ID=my-proj NOTIFICATION_CHANNEL=projects/.../notificationChannels/123 ./deploy/observability.sh
set -euo pipefail
: "${PROJECT_ID:?set PROJECT_ID}"
SERVICE="${SERVICE:-selfheal-agent}"
BASE="resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${SERVICE}\""

declare -A METRICS=(
  [selfheal_pipeline_failures]="pipeline.failed"
  [selfheal_mrs_opened]="fix.mr_opened"
  [selfheal_fixes_verified]="fix.verified"
  [selfheal_fixes_blocked]="fix.blocked"
  [selfheal_retries]="pipeline.retried"
  [selfheal_approvals_requested]="approval.requested"
  [selfheal_incidents_exhausted]="incident.exhausted"
  [selfheal_agent_errors]="agent.error"
)

for name in "${!METRICS[@]}"; do
  filter="${BASE} AND jsonPayload.event=\"${METRICS[$name]}\""
  if gcloud logging metrics describe "$name" --project "$PROJECT_ID" >/dev/null 2>&1; then
    gcloud logging metrics update "$name" --project "$PROJECT_ID" --log-filter "$filter"
  else
    gcloud logging metrics create "$name" --project "$PROJECT_ID" --log-filter "$filter" \
      --description "Self-heal agent event ${METRICS[$name]}"
  fi
done

if [[ -n "${NOTIFICATION_CHANNEL:-}" ]]; then
  tmp=$(mktemp)
  # Alert 1: the agent itself is erroring. Alert 2: failure storm (anomaly signal: many
  # failed pipelines in a short window usually means a shared dependency or infra broke).
  cat >"$tmp" <<EOF
{
  "displayName": "Self-heal agent: errors or pipeline failure storm",
  "combiner": "OR",
  "notificationChannels": ["${NOTIFICATION_CHANNEL}"],
  "conditions": [
    {
      "displayName": "Agent errors > 0 in 10 min",
      "conditionThreshold": {
        "filter": "metric.type=\"logging.googleapis.com/user/selfheal_agent_errors\" AND resource.type=\"cloud_run_revision\"",
        "aggregations": [{"alignmentPeriod": "600s", "perSeriesAligner": "ALIGN_SUM"}],
        "comparison": "COMPARISON_GT", "thresholdValue": 0, "duration": "0s"
      }
    },
    {
      "displayName": "More than ${STORM_THRESHOLD:-10} failed pipelines in 15 min",
      "conditionThreshold": {
        "filter": "metric.type=\"logging.googleapis.com/user/selfheal_pipeline_failures\" AND resource.type=\"cloud_run_revision\"",
        "aggregations": [{"alignmentPeriod": "900s", "perSeriesAligner": "ALIGN_SUM", "crossSeriesReducer": "REDUCE_SUM"}],
        "comparison": "COMPARISON_GT", "thresholdValue": ${STORM_THRESHOLD:-10}, "duration": "0s"
      }
    }
  ]
}
EOF
  gcloud monitoring policies create --project "$PROJECT_ID" --policy-from-file "$tmp"
  rm -f "$tmp"
else
  echo "NOTIFICATION_CHANNEL not set: metrics created, alert policy skipped."
fi
