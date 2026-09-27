#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--check" ]]; then
  check_only=1
  shift
else
  check_only=0
fi
if [[ $# -ne 0 ]]; then
  echo "usage: services/nemotron/run.sh [--check]" >&2
  exit 2
fi

: "${DIMON_NEMOTRON_MODEL_PATH:?DIMON_NEMOTRON_MODEL_PATH is required}"
: "${DIMON_NEMO_SPEECH_PATH:?DIMON_NEMO_SPEECH_PATH is required}"
: "${DIMON_NEMO_SPEECH_MODEL_DIR:?DIMON_NEMO_SPEECH_MODEL_DIR is required}"
[[ -s "$DIMON_NEMOTRON_MODEL_PATH" ]] || { echo "NEMOTRON_ARTIFACT_NOT_FOUND" >&2; exit 1; }
[[ -x "$DIMON_NEMO_SPEECH_PATH" ]] || { echo "NEMOTRON_RUNTIME_NOT_EXECUTABLE" >&2; exit 1; }
[[ -d "$DIMON_NEMO_SPEECH_MODEL_DIR" ]] || { echo "NEMOTRON_MODEL_DIR_NOT_FOUND" >&2; exit 1; }

if (( check_only )); then
  echo "nemotron: READY"
  exit 0
fi

export NEMO_SPEECH_MODEL_DIR="$DIMON_NEMO_SPEECH_MODEL_DIR"
exec "$DIMON_NEMO_SPEECH_PATH" serve \
  --host 127.0.0.1 \
  --port 8004 \
  --no-ui \
  --asr-model nemotron-en \
  --device cuda:0 \
  --asr.streaming.rnnt_right_context 6
