#!/usr/bin/env bash
# Episteme build wrapper: generated EU medicine records (one ema_epar article per medicine)
# -> ema_epar staging parts. Thin shell over `python -m episteme.data.ema.medicine_records`.
# It reads the loaded ema_medicines and ema_epar_documents feeds from the database; no
# download. Load the parts afterwards with load_ema_epar.sh. A rerun with unchanged feed
# releases writes nothing (--force writes again).
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
[ -n "$PY" ] || die "ema_epar: python not found (set PYTHON=/path/to/python)"

exec "$PY" -m episteme.data.ema.medicine_records "$@"
