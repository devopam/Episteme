#!/usr/bin/env bash
# SP2 knowledge-graph wrapper: europepmc_manuscript raw dir -> episteme.article_cites
# / article_mesh (+ article_parts). Thin shell over `python -m
# episteme.data.graph_builder --source europepmc_manuscript --raw-dir
# <raw>/europepmc/manuscripts`. graph_builder.main() takes only --source/--raw-dir,
# so run_pipeline.sh passes NO extra args; "$@" is kept for forward-compat.
#
# NOTE ON LOCATION: see extract_europepmc_manuscript.sh in this same directory
# — run_pipeline.sh's extract/load/graph dispatch requires this flat
# scripts/data/europepmc_manuscript/ path (literal $SOURCE); --raw-dir below
# still points at the nested europepmc/manuscripts raw directory the
# downloader writes.
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

exec "$PY" -m episteme.data.graph_builder --source europepmc_manuscript \
    --raw-dir "${EPISTEME_RAW_ROOT:-./01_raw}/europepmc/manuscripts" "$@"
