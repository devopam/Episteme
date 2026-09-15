#!/usr/bin/env bash
# SP2 knowledge-graph wrapper: guidelines raw dir -> episteme.article_cites /
# article_mesh (+ article_parts). Thin shell over `python -m
# episteme.data.graph_builder --source guidelines --raw-dir <raw>/guidelines`.
# graph_builder.main() takes only --source/--raw-dir, so run_pipeline.sh passes
# NO extra args; "$@" is kept for forward-compat / direct invocation only.
#
# NOTE ON LOCATION: see extract_guidelines.sh in this same directory —
# guidelines has no parent grouping directory, so this flat
# scripts/data/guidelines/ path already matches run_pipeline.sh's
# "$HERE/$SOURCE/${STAGE}_${SOURCE}.sh" dispatch.
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

exec "$PY" -m episteme.data.graph_builder --source guidelines \
    --raw-dir "${EPISTEME_RAW_ROOT:-./01_raw}/guidelines" "$@"
