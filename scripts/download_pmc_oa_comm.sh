#!/usr/bin/env bash
# ============================================================
# Episteme – PMC Commercial OA (oa_comm) Downloader
# Wrapper that activates the local virtual environment and
# executes download_pmc_oa_comm.py.
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Activate virtual environment if present
if [[ -f "$PROJECT_ROOT/.venv/bin/activate" ]]; then
  source "$PROJECT_ROOT/.venv/bin/activate"
elif [[ -f "$PROJECT_ROOT/.venv/Scripts/activate" ]]; then
  source "$PROJECT_ROOT/.venv/Scripts/activate"
fi

PYTHON_EXE="python"
if ! command -v "$PYTHON_EXE" >/dev/null 2>&1; then
  PYTHON_EXE="python3"
fi

# Parameters
OUTPUT_ROOT="${1:-./01_raw/pmc/oa_comm}"
FORMATS="${2:-xml}"          # space-separated: xml | txt
DRY_RUN="${3:-false}"        # true = plan only
LIMIT="${4:-0}"              # limit count, 0 = unlimited

ARGS=("--output_dir" "$OUTPUT_ROOT" "--formats" $FORMATS "--limit" "$LIMIT")

if [[ "$DRY_RUN" == "true" ]]; then
  ARGS+=("--dry_run")
fi

echo "=================================================="
echo "PMC Commercial OA (oa_comm) Downloader (Wrapper)"
echo "Target root   : $OUTPUT_ROOT"
echo "Formats       : $FORMATS"
echo "Dry-run       : $DRY_RUN"
echo "Limit         : $LIMIT"
echo "=================================================="

"$PYTHON_EXE" "$SCRIPT_DIR/download_pmc_oa_comm.py" "${ARGS[@]}"
