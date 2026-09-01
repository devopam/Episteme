#!/usr/bin/env bash
# Episteme – PMC oa_comm extraction entrypoint
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RAW_DIR="${1:-./01_raw/pmc/oa_comm}"
PROCESSED_DIR="${2:-./02_processed}"
MAX_FILES="${3:-0}"
WORKERS="${4:-4}"

if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

PYTHON=python3
command -v python3 >/dev/null 2>&1 || PYTHON=python

# Expect PYTHONPATH to include .../src
if ! "$PYTHON" -c "import episteme.data.pmc.extract" 2>/dev/null; then
  if [[ -d "./src" ]]; then
    export PYTHONPATH="$(pwd)/src${PYTHONPATH:+:$PYTHONPATH}"
  fi
fi

echo "=================================================="
echo "PMC oa_comm extractor"
echo "RAW       : $RAW_DIR"
echo "PROCESSED : $PROCESSED_DIR"
echo "MAX_FILES : $MAX_FILES (0=all)"
echo "WORKERS   : $WORKERS"
echo "=================================================="

exec "$PYTHON" -m episteme.data.pmc.extract \
  --raw-dir "$RAW_DIR" \
  --processed-dir "$PROCESSED_DIR" \
  --max-files "$MAX_FILES" \
  --workers "$WORKERS"
