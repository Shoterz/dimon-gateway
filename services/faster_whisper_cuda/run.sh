#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--check" ]]; then
  check_only=1
  shift
else
  check_only=0
fi
if [[ $# -ne 0 ]]; then
  echo "usage: services/faster_whisper_cuda/run.sh [--check]" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
runtime_root="$(realpath -m "$repo_root/.runtime")"
: "${DIMON_PROJECT_PYTHON:?DIMON_PROJECT_PYTHON is required}"
: "${DIMON_FASTER_WHISPER_PYTHON:?DIMON_FASTER_WHISPER_PYTHON is required}"
: "${DIMON_FASTER_WHISPER_MODEL_PATH:?DIMON_FASTER_WHISPER_MODEL_PATH is required}"
: "${DIMON_GPU_OVERLORD_CLIENT_DIR:?DIMON_GPU_OVERLORD_CLIENT_DIR is required}"

[[ -x "$DIMON_PROJECT_PYTHON" ]] || { echo "PROJECT_RUNTIME_NOT_EXECUTABLE" >&2; exit 1; }
[[ -x "$DIMON_FASTER_WHISPER_PYTHON" ]] || { echo "CUDA_RUNTIME_NOT_EXECUTABLE" >&2; exit 1; }
[[ -s "$DIMON_FASTER_WHISPER_MODEL_PATH/model.bin" ]] || { echo "MEDIUM_MODEL_NOT_FOUND" >&2; exit 1; }
[[ -s "$DIMON_FASTER_WHISPER_MODEL_PATH/.dimon-artifact.json" ]] || { echo "MEDIUM_MODEL_MARKER_NOT_FOUND" >&2; exit 1; }
[[ -f "$DIMON_GPU_OVERLORD_CLIENT_DIR/gpu_overlord_client.py" ]] || { echo "GPU_OVERLORD_CLIENT_NOT_FOUND" >&2; exit 1; }

runtime_python_parent="$(realpath -e "$(dirname "$DIMON_FASTER_WHISPER_PYTHON")")"
model_path="$(realpath -e "$DIMON_FASTER_WHISPER_MODEL_PATH")"
case "$runtime_python_parent/" in
  "$runtime_root"/*) ;;
  *) echo "CUDA_RUNTIME_OUTSIDE_REPOSITORY" >&2; exit 1 ;;
esac
case "$model_path" in
  "$runtime_root"/*) ;;
  *) echo "MEDIUM_MODEL_OUTSIDE_REPOSITORY" >&2; exit 1 ;;
esac

if (( check_only )); then
  echo "faster_whisper_cuda: READY"
  exit 0
fi

export DIMON_FASTER_WHISPER_FRONTEND_HOST=127.0.0.1
export DIMON_FASTER_WHISPER_FRONTEND_PORT=8007
cd "$repo_root"
exec "$DIMON_PROJECT_PYTHON" -m services.faster_whisper_cuda.app
