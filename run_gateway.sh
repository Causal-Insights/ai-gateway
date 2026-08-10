#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

FOLLOW_LOGS=true
if [[ "${1:-}" == "--no-follow" ]]; then
  FOLLOW_LOGS=false
  shift
fi
if [[ $# -ne 0 ]]; then
  echo "Usage: $0 [--no-follow]" >&2
  exit 2
fi

# Ledger file must exist before bind-mount (otherwise Docker may create a directory).
touch seedance_task_ledger.jsonl

docker compose down
docker build --platform linux/amd64 -t ai-gateway-litellm:local .
docker compose up -d --wait postgres

echo
echo "Running checked LiteLLM migrations against the local database..."
docker compose run --rm --no-deps \
  -e RUN_LITELLM_MIGRATIONS=true \
  -e LITELLM_MIGRATIONS_ONLY=true \
  litellm

docker compose up -d --force-recreate --wait --wait-timeout 240

echo
echo "LiteLLM is healthy on http://localhost:4000"
echo "Start the local backend separately with ./start_mlx_vlm.sh"

if [[ "${FOLLOW_LOGS}" == "true" ]]; then
  docker compose logs -f litellm
fi
