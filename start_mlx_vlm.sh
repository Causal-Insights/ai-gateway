#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

MODEL="${MLX_VLM_MODEL:-mlx-community/gemma-4-31b-it-8bit}"
PORT="${MLX_VLM_PORT:-8080}"
PYENV_ENV="${MLX_VLM_PYENV:-env_mlx}"

if ! command -v pyenv >/dev/null 2>&1; then
  echo "pyenv is required but was not found in PATH." >&2
  exit 1
fi

export PYENV_VERSION="$PYENV_ENV"

if ! pyenv prefix >/dev/null 2>&1; then
  echo "Unable to activate pyenv environment '$PYENV_ENV'." >&2
  exit 1
fi

echo "Starting MLX-VLM on http://0.0.0.0:${PORT}"
echo "Model: ${MODEL}"
echo "Health check: http://localhost:${PORT}/health"
echo "OpenAI endpoint: http://localhost:${PORT}/v1/chat/completions"
echo

exec pyenv exec mlx_vlm.server --host 0.0.0.0 --port "$PORT" --model "$MODEL"
