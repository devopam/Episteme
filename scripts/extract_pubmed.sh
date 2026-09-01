#!/usr/bin/env bash
# ============================================================
# Episteme – PubMed extraction entrypoint (contract v1.1)
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Prefer repo layout: scripts/data -> repo root; EpistemeData may only have scripts
ROOT="$(cd "$SCRIPT_DIR/../.." 2>/dev/null && pwd || pwd)"
SRC_DIR=""
for cand in \
  "$ROOT/src" \
  "$SCRIPT_DIR/../../src" \
  "$SCRIPT_DIR/../src" \
  "$(pwd)/src" \
  "$(pwd)"
do
  if [[ -f "$cand/episteme/data/pubmed/extract.py" ]]; then
    SRC_DIR="$cand"
    break
  fi
done

if [[ -z "$SRC_DIR" ]]; then
  # script colocated with package path from artifacts drop
  if [[ -f "$SCRIPT_DIR/../../src/episteme/data/pubmed/extract.py" ]]; then
    SRC_DIR="$(cd "$SCRIPT_DIR/../../src" && pwd)"
  fi
fi

RAW_DIR="${1:-./01_raw/pubmed}"
PROCESSED_DIR="${2:-./02_processed}"
MAX_FILES="${3:-1}"
EXTRA_ARGS=("${@:4}")

if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
elif [[ -f "$ROOT/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
fi

PYTHON=python3
command -v python3 >/dev/null 2>&1 || PYTHON=python

export PYTHONPATH="${SRC_DIR:-${PYTHONPATH:-}}:${PYTHONPATH:-}"

echo "=================================================="
echo "PubMed extractor"
echo "RAW       : $RAW_DIR"
echo "PROCESSED : $PROCESSED_DIR"
echo "MAX_FILES : $MAX_FILES"
echo "PYTHONPATH: $PYTHONPATH"
echo "=================================================="

# Ensure pyarrow if possible (non-fatal)
"$PYTHON" -c "import pyarrow" 2>/dev/null || {
  echo "NOTE: pyarrow not installed — will fall back to JSONL if needed."
  echo "      pip install pyarrow   # recommended"
}

if [[ -n "$SRC_DIR" && -f "$SRC_DIR/episteme/data/pubmed/extract.py" ]]; then
  exec "$PYTHON" -m episteme.data.pubmed.extract \
    --raw-dir "$RAW_DIR" \
    --processed-dir "$PROCESSED_DIR" \
    --max-files "$MAX_FILES" \
    "${EXTRA_ARGS[@]}"
fi

# Fallback: direct path to extract.py next to common drop layouts
EXTRACT_PY="$SCRIPT_DIR/../../src/episteme/data/pubmed/extract.py"
if [[ -f "$EXTRACT_PY" ]]; then
  exec "$PYTHON" "$EXTRACT_PY" \
    --raw-dir "$RAW_DIR" \
    --processed-dir "$PROCESSED_DIR" \
    --max-files "$MAX_FILES" \
    "${EXTRA_ARGS[@]}"
fi

echo "ERROR: could not locate episteme.data.pubmed.extract" >&2
echo "Place package under src/episteme/... or set PYTHONPATH" >&2
exit 1
