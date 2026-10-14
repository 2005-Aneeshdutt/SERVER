#!/usr/bin/env bash
# Start the full stack with distillation enabled: capture, replay server, llama.cpp serving the
# distilled model, and the rollout. Models live outside the repo (DISTILL_MODELS_DIR).
set -euo pipefail
cd "$(dirname "$0")/.."
export CAPTURE_ENABLED=true
export REPLAY_BASE_URL=http://replay-llm:9100/v1
export LLAMACPP_BASE_URL=http://llamacpp:8080/v1
export DISTILL_ROLLOUT_ENABLED=true
export DISTILL_MODELS_DIR="${DISTILL_MODELS_DIR:-$HOME/distill-models}"
docker compose --profile distill up -d --no-build
docker compose restart nginx  # it resolves replica IPs at startup
