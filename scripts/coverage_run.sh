#!/bin/bash
# Fixed operator entrypoint for the coverage-guided question pipeline.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ "$#" != 2 || "$1" != ask || -z "$2" ]]; then
  echo 'Usage: coverage_run.sh ask QUESTION (OUT must name a new workspace)' >&2
  exit 2
fi
: "${OUT:?OUT must name a new research workspace}"
PYTHON="${CANARY_COVERAGE_PYTHON:-$PWD/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo "Missing Python environment: $PYTHON; run uv sync" >&2
  exit 2
fi
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
if [[ -n "${CANARY_RUN_SPEC:-}" ]]; then
  exec "$PYTHON" -m canary.research_bundle "$CANARY_RUN_SPEC" "$OUT"
fi
exec "$PYTHON" -m canary.cli ask "$2" --out "$OUT"
