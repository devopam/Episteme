#!/usr/bin/env bash
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/_lib/common.sh"
load_dotenv
require_env EPISTEME_ACTOR

# Prefer the project venv's interpreter -- a plain `python` on PATH cannot
# import episteme. Mirrors the discovery pattern in scripts/data/pmc/*.sh
# and scripts/data/run_pipeline.sh (Task 10).
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in "$HERE/../../.venv/Scripts/python.exe" "$HERE/../../.venv/bin/python" python; do
        command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
    done
fi
[ -n "$PY" ] || die "python not found (set PYTHON=/path/to/python)"

exec "$PY" -c "
import sys
from episteme.data.db.connection import connection
from episteme.audit_trail import verify
with connection() as c:
    bad = verify(c)
print('audit chain OK' if not bad else f'CHAIN BROKEN at seq {[b[\"seq\"] for b in bad]}')
sys.exit(0 if not bad else 1)
"
