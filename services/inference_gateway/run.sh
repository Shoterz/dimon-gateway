#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--check" ]]; then
  shift
  [[ $# -eq 0 ]] || exit 2
  python_bin="${DIMON_PROJECT_PYTHON:-}"
  [[ -n "$python_bin" && -x "$python_bin" ]] || { echo "GATEWAY_RUNTIME_NOT_EXECUTABLE" >&2; exit 1; }
  echo "inference_gateway: READY"
  exit 0
fi
[[ $# -eq 0 ]] || exit 2
: "${DIMON_PROJECT_PYTHON:?DIMON_PROJECT_PYTHON is required}"
exec "$DIMON_PROJECT_PYTHON" -m services.inference_gateway.cli
