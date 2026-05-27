#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Ledger file must exist before bind-mount (otherwise Docker may create a directory).
touch seedance_task_ledger.jsonl

docker compose down
docker compose up -d --force-recreate

echo

echo "LiteLLM is restarting on http://localhost:4000"
echo "Start the local backend separately with ./start_mlx_vlm.sh"

docker compose logs -f litellm