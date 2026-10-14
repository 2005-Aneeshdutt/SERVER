#!/usr/bin/env bash
# Convert a merged HF model (train/train_lora.py output) to a quantized GGUF for llama.cpp:
#   bash scripts/to_gguf.sh <models dir>/banking77-intent/v1
# Uses llama.cpp's own converter from its "full" Docker image, so nothing is installed locally.
# Q8_0: ~8 bits per weight, near-lossless for a 0.5B model and still small (~0.5 GB).
set -euo pipefail
MODEL_DIR="$(cd "$1" && (pwd -W 2>/dev/null || pwd))"  # Windows path for Docker Desktop on Git Bash
IMAGE="ghcr.io/ggml-org/llama.cpp:full-b11011"
MSYS_NO_PATHCONV=1 docker run --rm -v "$MODEL_DIR:/model" "$IMAGE" \
  --convert /model/hf --outtype q8_0 --outfile /model/model.gguf
sha256sum "$1/model.gguf" | tee "$1/model.gguf.sha256"
