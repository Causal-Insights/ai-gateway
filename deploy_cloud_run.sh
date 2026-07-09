#!/usr/bin/env bash
# Redeploy LiteLLM proxy to Google Cloud Run (build + push + deploy).
#
# Usage:
#   ./deploy_cloud_run.sh
#   PROJECT_ID=ai-gateway-495414 REGION=us-central1 ./deploy_cloud_run.sh --memory 2Gi --cpu 2
#
# Prerequisites:
#   - gcloud CLI authenticated (`gcloud auth login`)
#   - APIs enabled (run, artifactregistry, cloudbuild) — script can enable them if permitted
#   - Artifact Registry repo exists (script creates it if missing)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PROJECT_ID="${PROJECT_ID:-ai-gateway-495414}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-ai-gateway-proxy}"
AR_REPO="${AR_REPO:-ai-gateway}"
IMAGE_NAME="${IMAGE_NAME:-litellm-proxy}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

DATABASE_URL_SECRET="${DATABASE_URL_SECRET:-DATABASE_URL}"
LITELLM_MASTER_KEY_SECRET="${LITELLM_MASTER_KEY_SECRET:-LITELLM_MASTER_KEY}"
OPENAI_API_KEY_SECRET="${OPENAI_API_KEY_SECRET:-OPENAI_API_KEY}"
GROK_API_KEY_SECRET="${GROK_API_KEY_SECRET:-GROK_API_KEY}"
BYTEDANCE_API_KEY_SECRET="${BYTEDANCE_API_KEY_SECRET:-BYTEDANCE_API_KEY}"
ELEVENLABS_API_KEY_SECRET="${ELEVENLABS_API_KEY_SECRET:-ELEVENLABS_API_KEY}"

# Cloud Run defaults (tuned for LiteLLM startup; adjust as needed)
# Cloud Run --timeout caps any single request. The Seedance handler does a
# bounded-wait long-poll (default 240s) and then returns a task-id placeholder;
# clients resume with cheap GET-style polls. SEEDANCE_POLL_TIMEOUT_S is the
# upper bound for the explicit blocking opt-in (`async_submit=false`).
MEMORY="${MEMORY:-2Gi}"
CPU="${CPU:-2}"
TIMEOUT="${TIMEOUT:-1800}"
MIN_INSTANCES="${MIN_INSTANCES:-0}"
MAX_INSTANCES="${MAX_INSTANCES:-10}"
ALLOW_UNAUTHENTICATED="${ALLOW_UNAUTHENTICATED:-false}"
SEEDANCE_SYNC_WAIT_S="${SEEDANCE_SYNC_WAIT_S:-240}"
SEEDANCE_POLL_TIMEOUT_S="${SEEDANCE_POLL_TIMEOUT_S:-1200}"

RUNTIME_SA="${RUNTIME_SA:-}"

read_env_value() {
  local key="$1"
  local file="${2:-.env}"

  [[ -f "${file}" ]] || return 0

  sed -n "s/^${key}=//p" "${file}" \
    | tail -n 1 \
    | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

hash_value() {
  printf "%s" "$1" | shasum -a 256 | awk '{print $1}'
}

check_database_separation() {
  local cloud_database_url
  local legacy_local_database_url
  local effective_local_database_url
  local cloud_hash
  local legacy_hash
  local local_hash

  echo "==> Checking local/prod database separation..."
  if ! cloud_database_url="$(gcloud secrets versions access latest --secret="${DATABASE_URL_SECRET}" --project="${PROJECT_ID}" 2>/dev/null)"; then
    echo "ERROR: Unable to access Secret Manager secret '${DATABASE_URL_SECRET}' in project '${PROJECT_ID}'." >&2
    echo "       Create it or set DATABASE_URL_SECRET to the production database secret name." >&2
    exit 1
  fi

  cloud_hash="$(hash_value "${cloud_database_url}")"
  legacy_local_database_url="$(read_env_value DATABASE_URL)"
  effective_local_database_url="${LOCAL_DATABASE_URL:-$(read_env_value LOCAL_DATABASE_URL)}"
  effective_local_database_url="${effective_local_database_url:-postgresql://litellm:litellm_local@postgres:5432/litellm_local}"
  local_hash="$(hash_value "${effective_local_database_url}")"

  if [[ -n "${legacy_local_database_url}" ]]; then
    legacy_hash="$(hash_value "${legacy_local_database_url}")"
    if [[ "${legacy_local_database_url}" == "${cloud_database_url}" ]]; then
      echo "ERROR: .env DATABASE_URL matches the Cloud Run '${DATABASE_URL_SECRET}' secret." >&2
      echo "       Remove production DATABASE_URL from .env and use LOCAL_DATABASE_URL for local docker-compose." >&2
      echo "       matching_hash=${legacy_hash:0:12}" >&2
      exit 1
    fi
  fi

  if [[ "${effective_local_database_url}" == "${cloud_database_url}" ]]; then
    echo "ERROR: LOCAL_DATABASE_URL matches the Cloud Run '${DATABASE_URL_SECRET}' secret." >&2
    echo "       Local and deployed LiteLLM must use different databases." >&2
    echo "       matching_hash=${local_hash:0:12}" >&2
    exit 1
  fi

  echo "==> DB separation OK (local=${local_hash:0:12}, prod=${cloud_hash:0:12})"
}

usage() {
  sed -n '1,120p' "$0" | sed -n '2,/^set -e/p' | tail -n +2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ID="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --service) SERVICE_NAME="$2"; shift 2 ;;
    --repo) AR_REPO="$2"; shift 2 ;;
    --image) IMAGE_NAME="$2"; shift 2 ;;
    --tag) IMAGE_TAG="$2"; shift 2 ;;
    --memory) MEMORY="$2"; shift 2 ;;
    --cpu) CPU="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --min-instances) MIN_INSTANCES="$2"; shift 2 ;;
    --max-instances) MAX_INSTANCES="$2"; shift 2 ;;
    --allow-unauthenticated) ALLOW_UNAUTHENTICATED="true"; shift ;;
    --runtime-sa) RUNTIME_SA="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${IMAGE_NAME}:${IMAGE_TAG}"

echo "==> Project:       ${PROJECT_ID}"
echo "==> Region:        ${REGION}"
echo "==> Service:       ${SERVICE_NAME}"
echo "==> Image:         ${IMAGE_URI}"
echo "==> Memory/CPU:    ${MEMORY} / ${CPU}"
echo "==> Timeout:       ${TIMEOUT}s"
echo "==> Seedance sync wait: ${SEEDANCE_SYNC_WAIT_S}s"
echo "==> Seedance poll cap:  ${SEEDANCE_POLL_TIMEOUT_S}s"
echo "==> Database secret: ${DATABASE_URL_SECRET}"
echo "==> Unauthenticated access: ${ALLOW_UNAUTHENTICATED}"

gcloud config set project "${PROJECT_ID}" >/dev/null

check_database_separation

echo "==> Ensuring APIs are enabled..."
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  --quiet

if ! gcloud artifacts repositories describe "${AR_REPO}" --location="${REGION}" >/dev/null 2>&1; then
  echo "==> Creating Artifact Registry repo '${AR_REPO}'..."
  gcloud artifacts repositories create "${AR_REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="ai-gateway images" \
    --quiet
fi

echo "==> Building image via Cloud Build..."
gcloud builds submit --tag "${IMAGE_URI}"

DEPLOY_ARGS=(
  run deploy "${SERVICE_NAME}"
  --image "${IMAGE_URI}"
  --region "${REGION}"
  --platform managed
  --port 8080
  --memory "${MEMORY}"
  --cpu "${CPU}"
  --timeout "${TIMEOUT}"
  --min-instances "${MIN_INSTANCES}"
  --max-instances "${MAX_INSTANCES}"
  --update-env-vars "SEEDANCE_SYNC_WAIT_S=${SEEDANCE_SYNC_WAIT_S},SEEDANCE_POLL_TIMEOUT_S=${SEEDANCE_POLL_TIMEOUT_S}"
  --set-secrets "DATABASE_URL=${DATABASE_URL_SECRET}:latest,LITELLM_MASTER_KEY=${LITELLM_MASTER_KEY_SECRET}:latest,OPENAI_API_KEY=${OPENAI_API_KEY_SECRET}:latest,GROK_API_KEY=${GROK_API_KEY_SECRET}:latest,BYTEDANCE_API_KEY=${BYTEDANCE_API_KEY_SECRET}:latest,ELEVENLABS_API_KEY=${ELEVENLABS_API_KEY_SECRET}:latest"
)

if [[ -n "${RUNTIME_SA}" ]]; then
  DEPLOY_ARGS+=(--service-account "${RUNTIME_SA}")
fi

if [[ "${ALLOW_UNAUTHENTICATED}" == "true" ]]; then
  DEPLOY_ARGS+=(--allow-unauthenticated)
else
  DEPLOY_ARGS+=(--no-allow-unauthenticated)
fi

echo "==> Deploying to Cloud Run..."
gcloud "${DEPLOY_ARGS[@]}"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" --format='value(status.url)')"
echo
echo "Deployed: ${SERVICE_URL}"
echo
echo "Smoke test (public service + LiteLLM key as Bearer):"
echo "  curl -sS -H \"Authorization: Bearer \$LITELLM_MASTER_KEY\" \\"
echo "    -H \"Content-Type: application/json\" \\"
echo "    -X POST \"${SERVICE_URL}/v1/chat/completions\" \\"
echo "    -d '{\"model\":\"gpt-5.4-mini\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'"
echo
echo "Smoke test (private service): use Google ID token in Authorization and pass LiteLLM key via api-key header."
echo "See README.md for details."
