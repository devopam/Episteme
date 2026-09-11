#!/usr/bin/env bash
# SP2 literature-extract wrapper: epfl-llm/guidelines HF dataset files ->
# staging shards. Thin shell over `python -m
# episteme.data.guidelines.extract_guidelines`. Accepts only --max-files N and
# --force (forwarded via "$@"); run_pipeline.sh builds that arg list
# (lit_extract_args) — NOT the SP3 download wrapper_args superset.
#
# NOTE ON LOCATION: unlike europepmc_manuscript/europepmc_preprint (Tasks 7/8),
# guidelines has no parent grouping directory — WRAPPER["guidelines"] in
# run_pipeline.sh already points at the flat "guidelines/download_guidelines.sh"
# (one directory level, not two). run_pipeline.sh's extract/load/graph dispatch
# is hardcoded to "$HERE/$SOURCE/${STAGE}_${SOURCE}.sh" — for SOURCE=guidelines
# that resolves to this same scripts/data/guidelines/ directory as the download
# wrapper, so no flat-vs-nested split applies here.
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

exec "$PY" -m episteme.data.guidelines.extract_guidelines "$@"
