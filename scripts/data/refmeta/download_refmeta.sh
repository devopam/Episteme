#!/usr/bin/env bash
# Reference code lists: UCUM and ISO 639-2, and EDQM when the keys are set.
# LOINC and ISO 3166 are owner-placed and are not fetched here.
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

args=()
[ "${EPISTEME_DRY_RUN:-0}" = "1" ] && args+=(--dry-run)
exec "$PY" -m episteme.data.refmeta.download "${args[@]}" "$@"
