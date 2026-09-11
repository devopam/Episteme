#!/usr/bin/env bash
# Episteme SP2 literature-enrich wrapper: Europe PMC lite_metadata feed ->
# episteme.articles (journal/year/mesh UPDATE). Thin shell over `python -m
# episteme.data.europepmc.lite_metadata.enrich_from_lite`. Same directory
# depth as download_europepmc_lite.sh in this same directory.
#
# NOTE ON LOCATION: like Task 11's europepmc_id_mappings `load` exception,
# europepmc_lite is NOT in LIT_SOURCES (it has no extract/graph stage) --
# run_pipeline.sh special-cases `europepmc_lite enrich` to this nested path.
# See run_pipeline.sh's `enrich)` arms (early-validation block + dispatch
# block) for the two-arm exception.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../_lib/common.sh
. "$HERE/../../_lib/common.sh"
load_dotenv
require_env EPISTEME_ACTOR

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in "$HERE/../../../../.venv/Scripts/python.exe" "$HERE/../../../../.venv/bin/python" python; do
        command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
    done
fi
[ -n "$PY" ] || die "python not found (set PYTHON=/path/to/python)"

exec "$PY" -m episteme.data.europepmc.lite_metadata.enrich_from_lite "$@"
