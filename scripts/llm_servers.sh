#!/usr/bin/env bash
# Start the local model servers (llama.cpp's llama-server, taken from LM Studio's backend). CPU-only.
# chat: :8080 Qwen3.5-4B with thinking off   embeddings: :8081 nomic-embed-text-v1.5
# LM Studio's own server can't switch Qwen3.5 thinking off, so we run llama-server directly.
set -euo pipefail
B=$(ls -d "$HOME"/.lmstudio/extensions/backends/llama.cpp-linux-x86_64-avx2-* | sort -V | tail -1)
MODELS=${LMSTUDIO_MODELS:-/media/xlr8/6A025A89025A59E1/Users/karan/.lmstudio/models}
export LD_LIBRARY_PATH=$B
"$B/llama-server" -m "$MODELS/lmstudio-community/Qwen3.5-4B-GGUF/Qwen3.5-4B-Q4_K_M.gguf" --port 8080 --host 127.0.0.1 \
  -c 8192 -t 8 --parallel 1 --reasoning off --alias qwen3.5-4b &
"$B/llama-server" -m "$HOME"/.lmstudio/apps/bionic/.internal/bundled-models/nomic-ai/nomic-embed-text-v1.5-GGUF/nomic-embed-text-v1.5.Q4_K_M.gguf \
  --port 8081 --host 127.0.0.1 --embedding --pooling mean -c 2048 --alias nomic-embed &
trap 'kill 0' EXIT
wait
