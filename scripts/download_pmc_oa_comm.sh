#!/usr/bin/env bash
# ============================================================
# Episteme – PMC Commercial OA (oa_comm) Downloader
# Wrapper → python -m episteme.data.pmc.download_pmc
#
# Commercial only (CC0 / CC BY / CC BY-SA / CC BY-ND).
# Prefers oa_comm filelist if present; else ESearch + metadata verify.
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." 2>/dev/null && pwd || echo "$SCRIPT_DIR")"

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

# Make the episteme package importable when running from a source checkout
export PYTHONPATH="$PROJECT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

OUTPUT_ROOT="${1:-./01_raw/pmc/oa_comm}"
FORMATS="${2:-xml}"          # e.g. xml | "xml txt"
DRY_RUN="${3:-false}"        # true | false
LIMIT="${4:-10}"             # 0 = all (large!); default 10 for safe sample

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

exec "$PYTHON_EXE" -m episteme.data.pmc.download_pmc "${ARGS[@]}"
