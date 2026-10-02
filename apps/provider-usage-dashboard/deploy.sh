#!/usr/bin/env bash
# Independent dashboard deployment. Does not call the Gateway deployment script.
set -euo pipefail
cd "$(dirname "$0")"
PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-ai-gateway-495414}"
REGION="${REGION:-us-central1}"
DATABASE="${FIRESTORE_DATABASE:-provider-usage-dashboard}"
SERVICE=provider-usage-dashboard
JOB=provider-usage-dashboard-sync
WEB_SA="provider-dashboard-web@${PROJECT_ID}.iam.gserviceaccount.com"
SYNC_SA="provider-dashboard-sync@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_SA="provider-dashboard-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
: "${IAP_MEMBER:?Set IAP_MEMBER to the authorized user:email or group:email}"
GC=(gcloud --quiet --project "$PROJECT_ID")
PROJECT_NUMBER="$("${GC[@]}" projects describe "$PROJECT_ID" --format='value(projectNumber)')"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/ai-gateway/provider-usage-dashboard:$(date -u +%Y%m%d%H%M%S)"

"${GC[@]}" services enable firestore.googleapis.com run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com iap.googleapis.com cloudresourcemanager.googleapis.com
for account in provider-dashboard-web provider-dashboard-sync provider-dashboard-scheduler; do
  if ! "${GC[@]}" iam service-accounts describe "${account}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1; then
    "${GC[@]}" iam service-accounts create "$account" --display-name="Provider Usage Dashboard: ${account}"
  fi
done
if ! "${GC[@]}" firestore databases describe --database="$DATABASE" >/dev/null 2>&1; then
  "${GC[@]}" firestore databases create --database="$DATABASE" --location="$REGION" --type=firestore-native --delete-protection
fi
CONDITION="expression=resource.name == 'projects/${PROJECT_ID}/databases/${DATABASE}',title=provider-usage-dashboard"
"${GC[@]}" projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$WEB_SA" --role=roles/datastore.viewer --condition="$CONDITION" >/dev/null
"${GC[@]}" projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SYNC_SA" --role=roles/datastore.user --condition="$CONDITION" >/dev/null
"${GC[@]}" projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SYNC_SA" --role=roles/bigquery.jobUser --condition=None >/dev/null

# Attach only separately published reporting secrets. Missing integrations remain ×.
SECRET_FLAGS=()
for name in OPENAI_USAGE_API_KEY XAI_USAGE_API_KEY BYTEPLUS_BILLING_ACCESS_KEY_ID BYTEPLUS_BILLING_SECRET_ACCESS_KEY ELEVENLABS_USAGE_API_KEY MINIMAX_USAGE_API_KEY GATEWAY_REPORTING_DATABASE_URL MAGICLENS_REPORTING_DATABASE_URL; do
  secret="provider-usage-dashboard-$(printf '%s' "$name" | tr '[:upper:]_' '[:lower:]-')"
  if "${GC[@]}" secrets describe "$secret" >/dev/null 2>&1; then
    "${GC[@]}" secrets add-iam-policy-binding "$secret" --member="serviceAccount:$SYNC_SA" --role=roles/secretmanager.secretAccessor >/dev/null
    SECRET_FLAGS+=(--update-secrets "${name}=${secret}:latest")
  fi
done
"${GC[@]}" builds submit . --tag "$IMAGE"
JOB_ENV="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},FIRESTORE_DATABASE=${DATABASE}"
for name in XAI_TEAM_ID GOOGLE_BILLING_TABLE GOOGLE_BILLING_MAX_BYTES; do
  if [[ -n "${!name:-}" ]]; then JOB_ENV+=",${name}=${!name}"; fi
done
"${GC[@]}" run jobs deploy "$JOB" --image="$IMAGE" --region="$REGION" --service-account="$SYNC_SA" \
  --args=sync --tasks=1 --parallelism=1 --max-retries=0 --task-timeout=1200s --cpu=1 --memory=512Mi \
  --set-env-vars="$JOB_ENV" "${SECRET_FLAGS[@]}"
for member in "$WEB_SA" "$SCHEDULER_SA"; do
  "${GC[@]}" run jobs add-iam-policy-binding "$JOB" --region="$REGION" --member="serviceAccount:$member" --role=roles/run.invoker >/dev/null
done
"${GC[@]}" run deploy "$SERVICE" --image="$IMAGE" --region="$REGION" --service-account="$WEB_SA" \
  --no-allow-unauthenticated --iap --min=0 --max=2 --cpu=1 --memory=512Mi \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},FIRESTORE_DATABASE=${DATABASE},DASHBOARD_SYNC_JOB=projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB},IAP_AUDIENCE=/projects/${PROJECT_NUMBER}/locations/${REGION}/services/${SERVICE}"
"${GC[@]}" run services add-iam-policy-binding "$SERVICE" --region="$REGION" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com" --role=roles/run.invoker >/dev/null
"${GC[@]}" iap web add-iam-policy-binding --resource-type=cloud-run --service="$SERVICE" --region="$REGION" \
  --member="$IAP_MEMBER" --role=roles/iap.httpsResourceAccessor >/dev/null
SCHEDULE_FLAGS=(--location="$REGION" --schedule='17 * * * *' --time-zone=UTC \
  --uri="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB}:run" \
  --http-method=POST --oauth-service-account-email="$SCHEDULER_SA")
if "${GC[@]}" scheduler jobs describe "$JOB" --location="$REGION" >/dev/null 2>&1; then
  "${GC[@]}" scheduler jobs update http "$JOB" "${SCHEDULE_FLAGS[@]}"
else
  "${GC[@]}" scheduler jobs create http "$JOB" "${SCHEDULE_FLAGS[@]}"
fi
"${GC[@]}" run jobs execute "$JOB" --region="$REGION"
"${GC[@]}" run services describe "$SERVICE" --region="$REGION" --format='value(status.url)'
