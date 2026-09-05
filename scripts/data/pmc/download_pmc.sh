#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/../_lib/common.sh"
load_dotenv
require_env EPISTEME_ACTOR

# Prefer the project venv's interpreter (a non-developer running this script
# has usually not activated it); PYTHON overrides; bare `python` is the last
# resort. Mirrors the PSQL-discovery pattern in scripts/data/db/*.sh.
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in "$HERE/../../../.venv/Scripts/python.exe" "$HERE/../../../.venv/bin/python" python; do
        command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
    done
fi
[ -n "$PY" ] || die "python not found (set PYTHON=/path/to/python)"

exec "$PY" -m episteme.data.pmc.download_pmc "$@"
