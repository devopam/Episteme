#!/usr/bin/env bash
# Episteme load wrapper: ema_epar staging parts -> episteme.articles / article_body.
# Thin shell over `python -m episteme.data.load_articles --source ema_epar`, then the retire
# step for the rows of deleted parts (only after a load that exited 0).
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

"$PY" -m episteme.data.load_articles --source ema_epar "$@" || exit $?

# Retire the rows of deleted parts, only after a load that exited 0.
exec "$PY" -m episteme.data.ema.retire "$@"
