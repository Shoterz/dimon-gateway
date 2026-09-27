#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--check" ]]; then
  check_only=1
  shift
else
  check_only=0
fi
if [[ $# -ne 0 ]]; then
  echo "usage: services/qwen/run.sh [--check]" >&2
  exit 2
fi

: "${DIMON_QWEN_MODEL_PATH:?DIMON_QWEN_MODEL_PATH is required}"
: "${DIMON_LLAMA_SERVER_PATH:?DIMON_LLAMA_SERVER_PATH is required}"
[[ -s "$DIMON_QWEN_MODEL_PATH" ]] || { echo "QWEN_ARTIFACT_NOT_FOUND" >&2; exit 1; }
[[ -x "$DIMON_LLAMA_SERVER_PATH" ]] || { echo "QWEN_RUNTIME_NOT_EXECUTABLE" >&2; exit 1; }

if (( check_only )); then
  echo "qwen: READY"
  exit 0
fi

exec "$DIMON_LLAMA_SERVER_PATH" \
  --model "$DIMON_QWEN_MODEL_PATH" \
  --host 127.0.0.1 \
  --port 8080 \
  --ctx-size 16384 \
  --n-gpu-layers auto \
  --parallel 1 \
  --flash-attn auto \
  --cache-type-k q4_0 \
  --cache-type-v q4_0 \
  --batch-size 512 \
  --ubatch-size 128 \
  --context-shift
