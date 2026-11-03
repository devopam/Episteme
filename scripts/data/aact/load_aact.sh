#!/usr/bin/env bash
# AACT load wrapper: the newest <YYYYMMDD>_*.zip under the aact raw folder -> its own
# schema aact_r<YYYYMMDD>; the views of schema aact switch to it only after the checks pass.
# Thin shell over `python -m episteme.data.aact.load_aact`.
# run_pipeline.sh forwards --force --reason R when --force was given.
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
[ -n "$PY" ] || die "aact: python not found (set PYTHON=/path/to/python)"

exec "$PY" -m episteme.data.aact.load_aact "$@"
