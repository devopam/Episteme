#!/usr/bin/env bash
# ============================================================
# Episteme – PMC Commercial OA (oa_comm) Downloader
# Wrapper → download_pmc_oa_comm.py
#
# Commercial only (CC0 / CC BY / CC BY-SA / CC BY-ND).
# Prefers oa_comm filelist if present; else ESearch + metadata verify.
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." 2>/dev/null && pwd || echo "$SCRIPT_DIR")"

# venv (repo root or cwd)
if [[ -f "$PROJECT_ROOT/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.venv/bin/activate"
elif [[ -f "$SCRIPT_DIR/../.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/../.venv/bin/activate"
elif [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

PYTHON_EXE="python3"
command -v python3 >/dev/null 2>&1 || PYTHON_EXE="python"

OUTPUT_ROOT="${1:-./01_raw/pmc/oa_comm}"
FORMATS="${2:-xml}"          # e.g. xml | "xml txt"
DRY_RUN="${3:-false}"        # true | false
LIMIT="${4:-10}"             # 0 = all (large!); default 10 for safe sample

PY_SCRIPT="$SCRIPT_DIR/download_pmc_oa_comm.py"
if [[ ! -f "$PY_SCRIPT" ]]; then
  # allow sibling layout: scripts/data/*.sh + same dir .py
  if [[ -f "$SCRIPT_DIR/../download_pmc_oa_comm.py" ]]; then
    PY_SCRIPT="$SCRIPT_DIR/../download_pmc_oa_comm.py"
  else
    echo "ERROR: download_pmc_oa_comm.py not found next to wrapper" >&2
    exit 1
  fi
fi

ARGS=(--output_dir "$OUTPUT_ROOT" --limit "$LIMIT")

# formats may be space-separated
# shellcheck disable=SC2206
FMT_ARR=($FORMATS)
ARGS+=(--formats "${FMT_ARR[@]}")

if [[ "$DRY_RUN" == "true" ]]; then
  ARGS+=(--dry_run)
fi

if [[ -n "${NCBI_API_KEY:-}" ]]; then
  ARGS+=(--api_key "$NCBI_API_KEY")
fi

echo "=================================================="
echo "PMC Commercial OA Downloader"
echo "Target : $OUTPUT_ROOT"
echo "Formats: $FORMATS"
echo "Dry-run: $DRY_RUN"
echo "Limit  : $LIMIT   (0 = full commercial set – very large)"
echo "Filter : commercial licenses only (no NC, no author_manuscript mix)"
echo "=================================================="

"$PYTHON_EXE" "$PY_SCRIPT" "${ARGS[@]}"
