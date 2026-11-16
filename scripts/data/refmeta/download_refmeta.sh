#!/usr/bin/env bash
# Reference code lists: UCUM and ISO 639-2 (public files), ISO 3166 (from the
# installed pycountry), and EDQM and LOINC when their keys are set. --system NAME
# picks lists. EDQM and LOINC ask for their version first and fetch nothing that
# is already stored; nothing is retried.
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
