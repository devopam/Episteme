#!/usr/bin/env bash
# SP4 graph wrapper: mesh -- the only structured source with a graph_ wrapper
# (Task 2's run_pipeline.sh edit left this [ -f ] guard deliberately open for
# Task 10 to close). Thin shell over `python -m episteme.data.graph_builder
# --source mesh`, which derives episteme.mesh_hierarchy parent/child edges by
# re-parsing the raw MeSH descriptor XML under --raw-dir (Task 4). No extra
# args are forwarded from run_pipeline.sh's graph dispatch (graph_builder.main
# takes only --source/--raw-dir, PF-8), so "$@" here is normally empty.
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

exec "$PY" -m episteme.data.graph_builder --source mesh --raw-dir "${EPISTEME_RAW_ROOT:-./01_raw}/mesh" "$@"
