#!/usr/bin/env bash
# SP2 literature-extract wrapper: Europe PMC author-manuscript *.tar.gz
# archives -> staging shards. Thin shell over `python -m
# episteme.data.europepmc.manuscripts.extract_europepmc_manuscripts`. Accepts
# only --max-files N and --force (forwarded via "$@"); run_pipeline.sh builds
# that arg list (lit_extract_args) — NOT the SP3 download wrapper_args superset.
#
# NOTE ON LOCATION: run_pipeline.sh's extract/load/graph dispatch is
# hardcoded to "$HERE/$SOURCE/${STAGE}_${SOURCE}.sh" (no override table, unlike
# the download-stage WRAPPER array) — so for SOURCE=europepmc_manuscript this
# wrapper MUST live at scripts/data/europepmc_manuscript/ (flat, literal source
# name), even though the download wrapper and the python package both nest
# under scripts/data/europepmc/manuscripts/ and
# src/episteme/data/europepmc/manuscripts/ respectively. Verified empirically
# by Task 7 for the sibling europepmc_preprint source: putting this file under
# the nested dir makes run_pipeline.sh die 3 with "wrapper not found".
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"
load_dotenv
require_env EPISTEME_ACTOR

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in "$HERE/../../../.venv/Scripts/python.exe" "$HERE/../../../.venv/bin/python" python; do
        command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
    done
fi
[ -n "$PY" ] || die "python not found (set PYTHON=/path/to/python)"

exec "$PY" -m episteme.data.europepmc.manuscripts.extract_europepmc_manuscripts "$@"
