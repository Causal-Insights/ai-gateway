#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

docker compose down
docker compose up -d --force-recreate

echo

echo "LiteLLM is restarting on http://localhost:4000"
echo "Start the local backend separately with ./start_mlx_vlm.sh"

docker compose logs -f litellm