#!/usr/bin/env bash
# FDA substance load wrapper: openfda other/substance zip -> episteme.substance and
# episteme.substance_xref (UNII, names, RxCUI, ATC, active moieties).
# Thin shell over `python -m episteme.data.regulatory.load_substance`.
# Pass --force --reason R to reload a file that already has a load marker.
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
[ -n "$PY" ] || die "openfda: python not found (set PYTHON=/path/to/python)"

exec "$PY" -m episteme.data.regulatory.load_substance "$@"
