#!/usr/bin/env bash
# Build both images from a tar stream of the working tree.
#
# `docker compose build` works everywhere except where the repo lives in a OneDrive folder:
# Docker Desktop then reads OneDrive's placeholder stubs instead of the files ("invalid file
# request Dockerfile"). tar reads files through normal APIs, so streaming the context sidesteps it.
set -euo pipefail
cd "$(dirname "$0")/.."

context() {
  tar --exclude=./.venv --exclude=./.git --exclude=./web/node_modules --exclude=./web/dist \
      --exclude=./eval/data --exclude=./loadtest/results --exclude='*/__pycache__' -cf - .
}

context | docker build -t llm-gateway:local -f Dockerfile -
context | docker build -t llm-gateway-web:local -f docker/nginx/Dockerfile -
echo "built llm-gateway:local and llm-gateway-web:local; start with: docker compose up -d --no-build"
