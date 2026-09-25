#!/usr/bin/env bash
# Redeploy LiteLLM proxy to Google Cloud Run (build + push + deploy).
#
# Usage:
#   ./deploy_cloud_run.sh --tag <immutable-release-tag> --candidate-only
#   ./deploy_cloud_run.sh --tag <release> --image-uri <repository@sha256:digest> --candidate-only
#   PROJECT_ID=ai-gateway-495414 REGION=us-central1 ./deploy_cloud_run.sh --tag <immutable-release-tag>
# Candidate serving deployments include both services; migration candidates are
# gateway-only. Exact URLs, revisions and promotion commands are written to the
# release manifest. No candidate-only invocation promotes either service.
# Capacity overrides: --max-instances, --concurrency, --callback-max-instances,
# --callback-concurrency, --db-connection-budget and --db-overlap-connections.
# Pool overrides: GENERATION_DB_POOL_SIZE and CALLBACK_DB_POOL_SIZE.
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
CALLBACK_SERVICE_NAME="${CALLBACK_SERVICE_NAME:-ai-gateway-callbacks}"
AR_REPO="${AR_REPO:-ai-gateway}"
IMAGE_NAME="${IMAGE_NAME:-litellm-proxy}"
IMAGE_TAG="${IMAGE_TAG:-}"
DEPLOY_IMAGE_URI="${DEPLOY_IMAGE_URI:-}"
CANDIDATE_ONLY="${CANDIDATE_ONLY:-false}"
RUN_LITELLM_MIGRATIONS="${RUN_LITELLM_MIGRATIONS:-false}"
GROK_VIDEO_15_VIDEO_OPERATIONS_VERIFIED="${GROK_VIDEO_15_VIDEO_OPERATIONS_VERIFIED:-false}"
GENERATION_DB_POOL_SIZE="${GENERATION_DB_POOL_SIZE:-2}"
CALLBACK_DB_POOL_SIZE="${CALLBACK_DB_POOL_SIZE:-${GENERATION_DB_POOL_SIZE}}"
CONCURRENCY="${CONCURRENCY:-80}"
CALLBACK_CONCURRENCY="${CALLBACK_CONCURRENCY:-80}"
CALLBACK_MIN_INSTANCES="${CALLBACK_MIN_INSTANCES:-1}"
CALLBACK_MAX_INSTANCES="${CALLBACK_MAX_INSTANCES:-10}"
# Optional total DB envelope includes the Prisma limit read from DATABASE_URL,
# both async pools, and caller-declared existing/retained revision connections.
DB_CONNECTION_BUDGET="${DB_CONNECTION_BUDGET:-}"
DB_OVERLAP_CONNECTIONS="${DB_OVERLAP_CONNECTIONS:-0}"
RELEASE_MANIFEST_DIR="${RELEASE_MANIFEST_DIR:-local-tests/deployments}"

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
MIN_INSTANCES="${MIN_INSTANCES:-1}"
MAX_INSTANCES="${MAX_INSTANCES:-10}"
ALLOW_UNAUTHENTICATED="${ALLOW_UNAUTHENTICATED:-false}"
SEEDANCE_SYNC_WAIT_S="${SEEDANCE_SYNC_WAIT_S:-240}"
SEEDANCE_POLL_TIMEOUT_S="${SEEDANCE_POLL_TIMEOUT_S:-1200}"

RUNTIME_SA="${RUNTIME_SA:-}"
TASKS_SA_NAME="${TASKS_SA_NAME:-ai-gateway-tasks}"
POLL_QUEUE_NAME="${POLL_QUEUE_NAME:-ai-generation-polls}"
RECONCILE_JOB_NAME="${RECONCILE_JOB_NAME:-ai-generation-reconcile}"
CLEANUP_JOB_NAME="${CLEANUP_JOB_NAME:-ai-generation-cleanup}"

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
  PRISMA_CONNECTION_LIMIT="$(printf '%s' "${cloud_database_url}" | python3 -c 'import sys,urllib.parse; print(urllib.parse.parse_qs(urllib.parse.urlsplit(sys.stdin.read()).query).get("connection_limit", [""])[0])')"
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
    --image-uri) DEPLOY_IMAGE_URI="$2"; shift 2 ;;
    --memory) MEMORY="$2"; shift 2 ;;
    --cpu) CPU="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --min-instances) MIN_INSTANCES="$2"; shift 2 ;;
    --max-instances) MAX_INSTANCES="$2"; shift 2 ;;
    --concurrency) CONCURRENCY="$2"; shift 2 ;;
    --callback-max-instances) CALLBACK_MAX_INSTANCES="$2"; shift 2 ;;
    --callback-concurrency) CALLBACK_CONCURRENCY="$2"; shift 2 ;;
    --db-connection-budget) DB_CONNECTION_BUDGET="$2"; shift 2 ;;
    --db-overlap-connections) DB_OVERLAP_CONNECTIONS="$2"; shift 2 ;;
    --allow-unauthenticated) ALLOW_UNAUTHENTICATED="true"; shift ;;
    --candidate-only) CANDIDATE_ONLY="true"; shift ;;
    --run-migrations) RUN_LITELLM_MIGRATIONS="true"; shift ;;
    --runtime-sa) RUNTIME_SA="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "${IMAGE_TAG}" ]]; then
  echo "ERROR: --tag (or IMAGE_TAG) is required; mutable default tags are disabled." >&2
  exit 2
fi
if [[ "${IMAGE_TAG}" == "latest" ]]; then
  echo "ERROR: IMAGE_TAG=latest is not allowed. Use a unique release or commit tag." >&2
  exit 2
fi
if [[ "${RUN_LITELLM_MIGRATIONS}" == "true" && "${CANDIDATE_ONLY}" != "true" ]]; then
  echo "ERROR: --run-migrations requires --candidate-only." >&2
  exit 2
fi
if [[ "${RUN_LITELLM_MIGRATIONS}" == "true" ]]; then
  MIN_INSTANCES=1
  MAX_INSTANCES=1
fi
if [[ -n "${DEPLOY_IMAGE_URI}" && ! "${DEPLOY_IMAGE_URI}" =~ @sha256:[a-f0-9]{64}$ ]]; then
  echo "ERROR: --image-uri must identify an immutable repository@sha256 digest." >&2
  exit 2
fi
for name in MAX_INSTANCES CALLBACK_MAX_INSTANCES CONCURRENCY CALLBACK_CONCURRENCY GENERATION_DB_POOL_SIZE CALLBACK_DB_POOL_SIZE; do
  if [[ ! "${!name}" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: ${name} must be a positive integer." >&2
    exit 2
  fi
done
if [[ ! "${DB_OVERLAP_CONNECTIONS}" =~ ^[0-9]+$ || ( -n "${DB_CONNECTION_BUDGET}" && ! "${DB_CONNECTION_BUDGET}" =~ ^[1-9][0-9]*$ ) ]]; then
  echo "ERROR: DB connection budget/overlap must be nonnegative integers (budget positive)." >&2
  exit 2
fi

IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${IMAGE_NAME}:${IMAGE_TAG}"
SOURCE_COMMIT="$(git rev-parse HEAD)"
CONFIG_SHA256="$(shasum -a 256 litellm_config.yaml | awk '{print $1}')"
# Include tracked edits and nonignored new files without recording their contents.
SOURCE_STATE_SHA256="$(python3 -c 'import hashlib,pathlib,subprocess; h=hashlib.sha256(); names=sorted(set(subprocess.check_output(["git","ls-files","-z","--cached","--others","--exclude-standard"]).split(b"\0"))-{b""}); [(h.update(name+b"\0"),h.update((pathlib.Path(name.decode()).read_bytes() if pathlib.Path(name.decode()).is_file() else b"<missing>")),h.update(b"\0")) for name in names]; print(h.hexdigest())')"
RELEASE_TOKEN="$(hash_value "${IMAGE_TAG}")"
RELEASE_TOKEN="${RELEASE_TOKEN:0:12}"
DEPLOY_STAGE=serving
[[ "${RUN_LITELLM_MIGRATIONS}" != "true" ]] || DEPLOY_STAGE=migration
GATEWAY_TAG="${DEPLOY_STAGE}-${RELEASE_TOKEN}"
CALLBACK_TAG="callback-${RELEASE_TOKEN}"
LABELS="gateway-commit=${SOURCE_COMMIT},gateway-source=${SOURCE_STATE_SHA256:0:63},gateway-config=${CONFIG_SHA256:0:63}"
SOURCE_IDENTITY_SCOPE=deployment-checkout-only

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
echo "==> Candidate only: ${CANDIDATE_ONLY}"
echo "==> Run migrations: ${RUN_LITELLM_MIGRATIONS}"
echo "==> Grok Video 1.5 video operations verified: ${GROK_VIDEO_15_VIDEO_OPERATIONS_VERIFIED}"

gcloud config set project "${PROJECT_ID}" >/dev/null

check_database_separation

if [[ "${PRISMA_CONNECTION_LIMIT}" =~ ^[1-9][0-9]*$ ]]; then
  DB_CONNECTION_ENVELOPE=$(( MAX_INSTANCES * (GENERATION_DB_POOL_SIZE + PRISMA_CONNECTION_LIMIT) + DB_OVERLAP_CONNECTIONS ))
  if [[ "${RUN_LITELLM_MIGRATIONS}" != "true" ]]; then
    DB_CONNECTION_ENVELOPE=$(( DB_CONNECTION_ENVELOPE + CALLBACK_MAX_INSTANCES * CALLBACK_DB_POOL_SIZE ))
  fi
  echo "==> DB connection envelope: ${DB_CONNECTION_ENVELOPE} (includes declared overlap ${DB_OVERLAP_CONNECTIONS})"
  if [[ -n "${DB_CONNECTION_BUDGET}" && "${DB_CONNECTION_ENVELOPE}" -gt "${DB_CONNECTION_BUDGET}" ]]; then
    echo "ERROR: Requested instance/pool envelope exceeds DB_CONNECTION_BUDGET=${DB_CONNECTION_BUDGET}." >&2
    exit 2
  fi
else
  DB_CONNECTION_ENVELOPE=unknown
  echo "==> Prisma connection_limit is unspecified; total connection envelope is unknown."
  if [[ -n "${DB_CONNECTION_BUDGET}" ]]; then
    echo "ERROR: A combined DB budget requires an explicit connection_limit in the DATABASE_URL secret." >&2
    exit 2
  fi
fi

echo "==> Ensuring APIs are enabled..."
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  cloudtasks.googleapis.com \
  cloudscheduler.googleapis.com \
  iamcredentials.googleapis.com \
  --quiet

TASKS_SA_EMAIL="${TASKS_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "${TASKS_SA_EMAIL}" >/dev/null 2>&1; then
  echo "==> Creating Cloud Tasks delivery service account '${TASKS_SA_NAME}'..."
  gcloud iam service-accounts create "${TASKS_SA_NAME}" \
    --display-name="AI generation poll delivery" --quiet
fi

if ! gcloud tasks queues describe "${POLL_QUEUE_NAME}" --location="${REGION}" >/dev/null 2>&1; then
  echo "==> Creating rate-limited generation poll queue..."
  gcloud tasks queues create "${POLL_QUEUE_NAME}" \
    --location="${REGION}" \
    --max-dispatches-per-second=20 \
    --max-concurrent-dispatches=20 \
    --max-attempts=5 \
    --min-backoff=5s \
    --max-backoff=60s \
    --quiet
else
  gcloud tasks queues update "${POLL_QUEUE_NAME}" \
    --location="${REGION}" \
    --max-dispatches-per-second=20 \
    --max-concurrent-dispatches=20 \
    --max-attempts=5 \
    --min-backoff=5s \
    --max-backoff=60s \
    --quiet
fi

if ! gcloud artifacts repositories describe "${AR_REPO}" --location="${REGION}" >/dev/null 2>&1; then
  echo "==> Creating Artifact Registry repo '${AR_REPO}'..."
  gcloud artifacts repositories create "${AR_REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="ai-gateway images" \
    --quiet
fi

if [[ -z "${DEPLOY_IMAGE_URI}" ]]; then
  echo "==> Building image via Cloud Build..."
  gcloud builds submit --tag "${IMAGE_URI}"
  IMAGE_DIGEST="$(gcloud artifacts docker images describe "${IMAGE_URI}" --format='value(image_summary.digest)')"
  if [[ ! "${IMAGE_DIGEST}" =~ ^sha256:[a-f0-9]{64}$ ]]; then
    echo "ERROR: Built image did not resolve to an immutable digest." >&2
    exit 1
  fi
  DEPLOY_IMAGE_URI="${IMAGE_URI%:*}@${IMAGE_DIGEST}"
  SOURCE_IDENTITY_SCOPE=build-checkout
else
  # Reuse the migration image's recorded source/config identities when present.
  # Otherwise these hashes identify this deployment checkout, not the old image.
  MIGRATION_MANIFEST="${RELEASE_MANIFEST_DIR}/${RELEASE_TOKEN}-migration.json"
  if [[ -f "${MIGRATION_MANIFEST}" ]]; then
    IMAGE_PROVENANCE="$(python3 - "${MIGRATION_MANIFEST}" "${DEPLOY_IMAGE_URI}" <<'PY'
import json, re, sys
record = json.load(open(sys.argv[1]))
values = [record.get(key, '') for key in ('source_commit', 'source_state_sha256', 'config_sha256')]
if record.get('image_uri') == sys.argv[2] and record.get('source_identity_scope') == 'build-checkout' and all(re.fullmatch('[0-9a-f]{'+str(length)+'}', value) for value, length in zip(values, (40, 64, 64))):
    print(' '.join(values))
PY
    )"
    if [[ -n "${IMAGE_PROVENANCE}" ]]; then
      read -r SOURCE_COMMIT SOURCE_STATE_SHA256 CONFIG_SHA256 <<< "${IMAGE_PROVENANCE}"
      SOURCE_IDENTITY_SCOPE=reused-migration-image
      LABELS="gateway-commit=${SOURCE_COMMIT},gateway-source=${SOURCE_STATE_SHA256:0:63},gateway-config=${CONFIG_SHA256:0:63}"
    fi
  fi
fi

SET_SECRETS="DATABASE_URL=${DATABASE_URL_SECRET}:latest,LITELLM_MASTER_KEY=${LITELLM_MASTER_KEY_SECRET}:latest,OPENAI_API_KEY=${OPENAI_API_KEY_SECRET}:latest,GROK_API_KEY=${GROK_API_KEY_SECRET}:latest,BYTEDANCE_API_KEY=${BYTEDANCE_API_KEY_SECRET}:latest,ELEVENLABS_API_KEY=${ELEVENLABS_API_KEY_SECRET}:latest"

DEPLOY_ARGS=(
  run deploy "${SERVICE_NAME}"
  --image "${DEPLOY_IMAGE_URI}"
  --region "${REGION}"
  --platform managed
  --port 8080
  --memory "${MEMORY}"
  --cpu "${CPU}"
  --timeout "${TIMEOUT}"
  --min-instances "${MIN_INSTANCES}"
  --max-instances "${MAX_INSTANCES}"
  --concurrency "${CONCURRENCY}"
  --tag "${GATEWAY_TAG}"
  --update-labels "${LABELS}"
  --update-env-vars "SEEDANCE_SYNC_WAIT_S=${SEEDANCE_SYNC_WAIT_S},SEEDANCE_POLL_TIMEOUT_S=${SEEDANCE_POLL_TIMEOUT_S},RUN_LITELLM_MIGRATIONS=${RUN_LITELLM_MIGRATIONS},GROK_VIDEO_15_VIDEO_OPERATIONS_VERIFIED=${GROK_VIDEO_15_VIDEO_OPERATIONS_VERIFIED},GENERATION_DB_POOL_SIZE=${GENERATION_DB_POOL_SIZE}"
  --set-secrets "${SET_SECRETS}"
)

if [[ -n "${RUNTIME_SA}" ]]; then
  DEPLOY_ARGS+=(--service-account "${RUNTIME_SA}")
fi

if [[ "${ALLOW_UNAUTHENTICATED}" == "true" ]]; then
  DEPLOY_ARGS+=(--allow-unauthenticated)
else
  DEPLOY_ARGS+=(--no-allow-unauthenticated)
fi

if [[ "${CANDIDATE_ONLY}" == "true" ]]; then
  DEPLOY_ARGS+=(--no-traffic)
fi

echo "==> Deploying to Cloud Run..."
GATEWAY_REVISION="$(gcloud "${DEPLOY_ARGS[@]}" --format='value(status.latestCreatedRevisionName)')"

tagged_url() {
  gcloud run services describe "$1" --region "${REGION}" --format=json | python3 -c \
    'import json,sys; entries=json.load(sys.stdin)["status"].get("traffic",[]); match=next((x for x in entries if x.get("tag")==sys.argv[1] and x.get("revisionName")==sys.argv[2]),None); print(match["url"] if match else ""); sys.exit(0 if match and match.get("url") else 1)' "$2" "$3"
}

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" --format='value(status.url)')"
GATEWAY_CANDIDATE_URL="$(tagged_url "${SERVICE_NAME}" "${GATEWAY_TAG}" "${GATEWAY_REVISION}")"
CALLBACK_REVISION=""
CALLBACK_CANDIDATE_URL=""

write_manifest() {
  mkdir -p "${RELEASE_MANIFEST_DIR}"
  MANIFEST_PATH="${RELEASE_MANIFEST_DIR}/${RELEASE_TOKEN}-${DEPLOY_STAGE}.json"
  python3 - "${MANIFEST_PATH}" "${PROJECT_ID}" "${REGION}" "${IMAGE_TAG}" "${DEPLOY_IMAGE_URI}" \
    "${SOURCE_COMMIT}" "${SOURCE_STATE_SHA256}" "${CONFIG_SHA256}" "${DEPLOY_STAGE}" "${CANDIDATE_ONLY}" \
    "${SERVICE_NAME}" "${GATEWAY_REVISION}" "${GATEWAY_CANDIDATE_URL}" \
    "${CALLBACK_SERVICE_NAME}" "${CALLBACK_REVISION}" "${CALLBACK_CANDIDATE_URL}" \
    "${DB_CONNECTION_ENVELOPE}" "${DB_OVERLAP_CONNECTIONS}" "${CONCURRENCY}" "${CALLBACK_CONCURRENCY}" "${SOURCE_IDENTITY_SCOPE}" <<'PY'
import json, pathlib, sys
keys = ('project', 'region', 'release_tag', 'image_uri', 'source_commit', 'source_state_sha256', 'config_sha256',
        'stage', 'candidate_only', 'gateway_service', 'gateway_revision', 'gateway_candidate_url',
        'callback_service', 'callback_revision', 'callback_candidate_url', 'db_connection_envelope',
        'db_overlap_connections', 'gateway_concurrency', 'callback_concurrency', 'source_identity_scope')
pathlib.Path(sys.argv[1]).write_text(json.dumps(dict(zip(keys, sys.argv[2:])), indent=2) + '\n')
PY
  echo "Release manifest: ${MANIFEST_PATH}"
}

if [[ "${RUN_LITELLM_MIGRATIONS}" == "true" ]]; then
  write_manifest
  echo "Migration candidate (never promote): ${GATEWAY_REVISION} ${GATEWAY_CANDIDATE_URL}"
  echo "After the migration gate, create a separate serving candidate from ${DEPLOY_IMAGE_URI} with --image-uri and without --run-migrations."
  exit 0
fi
POLL_TARGET_URL="${SERVICE_URL}"
[[ "${CANDIDATE_ONLY}" != "true" ]] || POLL_TARGET_URL="${GATEWAY_CANDIDATE_URL}"

echo "==> Granting poll delivery access to the private gateway..."
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
  --region "${REGION}" \
  --member="serviceAccount:${TASKS_SA_EMAIL}" \
  --role="roles/run.invoker" \
  --quiet >/dev/null

CALLBACK_DEPLOY_ARGS=(
  run deploy "${CALLBACK_SERVICE_NAME}"
  --image "${DEPLOY_IMAGE_URI}"
  --region "${REGION}"
  --platform managed
  --port 8080
  --memory 512Mi
  --cpu 1
  --timeout 30
  --min-instances "${CALLBACK_MIN_INSTANCES}"
  --max-instances "${CALLBACK_MAX_INSTANCES}"
  --concurrency "${CALLBACK_CONCURRENCY}"
  --tag "${CALLBACK_TAG}"
  --update-labels "${LABELS}"
  --allow-unauthenticated
  --command uvicorn
  --args "callback_server:app,--host,0.0.0.0,--port,8080"
  --update-env-vars "GENERATION_POLL_QUEUE_PROJECT=${PROJECT_ID},GENERATION_POLL_QUEUE_LOCATION=${REGION},GENERATION_POLL_QUEUE_NAME=${POLL_QUEUE_NAME},GENERATION_POLL_TARGET_URL=${POLL_TARGET_URL},GENERATION_POLL_AUDIENCE=${SERVICE_URL},GENERATION_POLL_SERVICE_ACCOUNT_EMAIL=${TASKS_SA_EMAIL},GENERATION_DB_POOL_SIZE=${CALLBACK_DB_POOL_SIZE}"
  --set-secrets "DATABASE_URL=${DATABASE_URL_SECRET}:latest"
)
if [[ -n "${RUNTIME_SA}" ]]; then
  CALLBACK_DEPLOY_ARGS+=(--service-account "${RUNTIME_SA}")
fi
if [[ "${CANDIDATE_ONLY}" == "true" ]]; then
  CALLBACK_DEPLOY_ARGS+=(--no-traffic)
fi
echo "==> Deploying callback-only service..."
CALLBACK_REVISION="$(gcloud "${CALLBACK_DEPLOY_ARGS[@]}" --format='value(status.latestCreatedRevisionName)')"
CALLBACK_URL="$(gcloud run services describe "${CALLBACK_SERVICE_NAME}" --region "${REGION}" --format='value(status.url)')"
CALLBACK_CANDIDATE_URL="$(tagged_url "${CALLBACK_SERVICE_NAME}" "${CALLBACK_TAG}" "${CALLBACK_REVISION}")"
CALLBACK_TARGET_URL="${CALLBACK_URL}"
[[ "${CANDIDATE_ONLY}" != "true" ]] || CALLBACK_TARGET_URL="${CALLBACK_CANDIDATE_URL}"

echo "==> Configuring durable job dispatch on the gateway..."
CONFIGURE_ARGS=(run services update "${SERVICE_NAME}" --region "${REGION}" --tag "${GATEWAY_TAG}"
  --update-env-vars "GATEWAY_PUBLIC_BASE_URL=${POLL_TARGET_URL},GENERATION_CALLBACK_BASE_URL=${CALLBACK_TARGET_URL},GENERATION_POLL_QUEUE_PROJECT=${PROJECT_ID},GENERATION_POLL_QUEUE_LOCATION=${REGION},GENERATION_POLL_QUEUE_NAME=${POLL_QUEUE_NAME},GENERATION_POLL_TARGET_URL=${POLL_TARGET_URL},GENERATION_POLL_AUDIENCE=${SERVICE_URL},GENERATION_POLL_SERVICE_ACCOUNT_EMAIL=${TASKS_SA_EMAIL}"
  --format='value(status.latestCreatedRevisionName)' --quiet)
[[ "${CANDIDATE_ONLY}" != "true" ]] || CONFIGURE_ARGS+=(--no-traffic)
GATEWAY_REVISION="$(gcloud "${CONFIGURE_ARGS[@]}")"
GATEWAY_CANDIDATE_URL="$(tagged_url "${SERVICE_NAME}" "${GATEWAY_TAG}" "${GATEWAY_REVISION}")"

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
DEFAULT_RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
DEPLOYED_RUNTIME_SA="$(gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" --format='value(spec.template.spec.serviceAccountName)')"
EFFECTIVE_RUNTIME_SA="${RUNTIME_SA:-${DEPLOYED_RUNTIME_SA:-${DEFAULT_RUNTIME_SA}}}"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${EFFECTIVE_RUNTIME_SA}" \
  --role="roles/cloudtasks.enqueuer" \
  --condition=None --quiet >/dev/null
gcloud iam service-accounts add-iam-policy-binding "${TASKS_SA_EMAIL}" \
  --member="serviceAccount:${EFFECTIVE_RUNTIME_SA}" \
  --role="roles/iam.serviceAccountUser" \
  --quiet >/dev/null

if [[ "${CANDIDATE_ONLY}" == "true" ]]; then
  write_manifest
  echo "Serving candidate: ${GATEWAY_REVISION} ${GATEWAY_CANDIDATE_URL}"
  echo "Callback candidate: ${CALLBACK_REVISION} ${CALLBACK_CANDIDATE_URL}"
  echo "After both candidates pass verification, promote explicitly (callback first):"
  echo "gcloud run services update-traffic ${CALLBACK_SERVICE_NAME} --project ${PROJECT_ID} --region ${REGION} --to-revisions ${CALLBACK_REVISION}=100"
  echo "gcloud run services update-traffic ${SERVICE_NAME} --project ${PROJECT_ID} --region ${REGION} --to-revisions ${GATEWAY_REVISION}=100"
  exit 0
fi

upsert_scheduler_job() {
  local name="$1"
  local schedule="$2"
  local path="$3"
  if gcloud scheduler jobs describe "${name}" --location="${REGION}" >/dev/null 2>&1; then
    gcloud scheduler jobs update http "${name}" --location="${REGION}" \
      --schedule="${schedule}" --uri="${SERVICE_URL}${path}" --http-method=POST \
      --oidc-service-account-email="${TASKS_SA_EMAIL}" --oidc-token-audience="${SERVICE_URL}" --quiet
  else
    gcloud scheduler jobs create http "${name}" --location="${REGION}" \
      --schedule="${schedule}" --uri="${SERVICE_URL}${path}" --http-method=POST \
      --oidc-service-account-email="${TASKS_SA_EMAIL}" --oidc-token-audience="${SERVICE_URL}" --quiet
  fi
}

echo "==> Configuring reconciliation and retention schedules..."
upsert_scheduler_job "${RECONCILE_JOB_NAME}" "* * * * *" "/internal/generation-jobs/reconcile"
upsert_scheduler_job "${CLEANUP_JOB_NAME}" "17 3 * * *" "/internal/generation-jobs/cleanup"
write_manifest
echo
echo "Deployed: ${SERVICE_URL}"
echo "Callbacks: ${CALLBACK_URL}"
echo
echo "Smoke test (public service + LiteLLM key as Bearer):"
echo "  curl -sS -H \"Authorization: Bearer \$LITELLM_MASTER_KEY\" \\"
echo "    -H \"Content-Type: application/json\" \\"
echo "    -X POST \"${SERVICE_URL}/v1/chat/completions\" \\"
echo "    -d '{\"model\":\"gpt-5.4-mini\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'"
echo
echo "Smoke test (private service): use Google ID token in Authorization and pass LiteLLM key via api-key header."
echo "See README.md for details."
