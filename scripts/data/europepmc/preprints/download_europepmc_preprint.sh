#!/usr/bin/env bash
# SP2 literature-download wrapper: Europe PMC preprint full text, REST-harvested
# per PPR id. Thin shell over `python -m
# episteme.data.europepmc.preprints.download_europepmc_preprints`.
#
# EBI DISCONTINUED the bulk preprint feed (2026-09-08 spike): the FTP dir now
# holds only pprid.txt.gz + a privacy notice. The python module fetches that id
# list and GETs each preprint's fullTextXML from the Europe PMC REST API.
#
# run_pipeline.sh drives `download` with wrapper_args = [--max-files N]
# [--force --reason R] [passthrough...] and signals --dry-run via
# EPISTEME_DRY_RUN. This loop forwards what the module understands
# (--max-files / --since / --raw-dir / --dry-run) and absorbs the rest.
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
        --since)     FWD+=(--since "${2:-}"); shift; [ $# -gt 0 ] && shift ;;
        --raw-dir)   FWD+=(--raw-dir "${2:-}"); shift; [ $# -gt 0 ] && shift ;;
        --force)     log INFO "download_europepmc_preprint.sh: --force is a no-op (resume via .harvest_state)"; shift ;;
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
