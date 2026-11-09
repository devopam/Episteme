#!/usr/bin/env bash
# SP2 literature-download wrapper: Europe PMC preprint bulk files
# (PPR<a>_PPR<b>.xml.gz). Thin shell over `python -m
# episteme.data.europepmc.preprints.download_europepmc_preprints`: the module reads
# the folder listing at EUROPEPMC_PREPRINT_BASE, skips files already here with the
# size the server states, and resumes a stopped file from its .part file. Files go to
# <raw>/europepmc/preprints_bulk/.
#
# run_pipeline.sh drives `download` with wrapper_args = [--max-files N]
# [--force --reason R] [passthrough...] and signals --dry-run via
# EPISTEME_DRY_RUN. This loop forwards what the module understands
# (--max-files / --force / --raw-dir / --dry-run) and absorbs the rest.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../_lib/common.sh
. "$HERE/../../_lib/common.sh"

FWD=()
[ "${EPISTEME_DRY_RUN:-0}" = "1" ] && FWD+=(--dry-run)

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)   FWD+=(--dry-run); shift ;;
        --max-files) FWD+=(--max-files "${2:-}"); shift; [ $# -gt 0 ] && shift ;;
        --raw-dir)   FWD+=(--raw-dir "${2:-}"); shift; [ $# -gt 0 ] && shift ;;
        --force)     FWD+=(--force); shift ;;
        --reason)    shift; [ $# -gt 0 ] && shift ;;   # run_pipeline already enforced it
        *)           log WARN "download_europepmc_preprint.sh: ignoring $1"; shift ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR EUROPEPMC_PREPRINT_BASE

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in "$HERE/../../../../.venv/Scripts/python.exe" "$HERE/../../../../.venv/bin/python" python; do
        command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
    done
fi
[ -n "$PY" ] || die "python not found (set PYTHON=/path/to/python)"

exec "$PY" -m episteme.data.europepmc.preprints.download_europepmc_preprints "${FWD[@]}"
