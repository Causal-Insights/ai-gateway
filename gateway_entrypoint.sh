#!/bin/sh

set -eu

if [ "${RUN_LITELLM_MIGRATIONS:-false}" = "true" ]; then
  if [ -z "${DATABASE_URL:-}" ]; then
    echo "RUN_LITELLM_MIGRATIONS=true requires DATABASE_URL" >&2
    exit 2
  fi
  echo "Running LiteLLM database migrations with checks enabled..."
  litellm \
    --config "${CONFIG_FILE_PATH:-/app/litellm_config.yaml}" \
    --skip_server_startup \
    --enforce_prisma_migration_check \
    --use_v2_migration_resolver

  if [ "${LITELLM_MIGRATIONS_ONLY:-false}" = "true" ]; then
    echo "LiteLLM database migrations completed successfully."
    exit 0
  fi
fi

exec uvicorn gateway_server:app --host 0.0.0.0 --port "${PORT:-8080}"
