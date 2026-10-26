#!/usr/bin/env bash
# SP4 structured-load wrapper: pubchem structured staging shards -> Postgres,
# then retire the rows of deleted parts. Thin shell over
# `python -m episteme.data.load_articles --source pubchem` followed by
# `python -m episteme.data.pubchem.retire`. run_pipeline.sh forwards load_args
# (--force --reason R when --force was given, --skip-unit NAME --reason R when
# skipping, else nothing) via "$@" to BOTH calls; retire.py accepts and ignores
# them. Not `exec`'d for the first call: the retire step runs only after
# load_articles has exited 0 (a failed load must never retire anything).
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

"$PY" -m episteme.data.load_articles --source pubchem "$@" || exit $?

exec "$PY" -m episteme.data.pubchem.retire "$@"
