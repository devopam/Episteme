#!/usr/bin/env bash
# SP7 structured-load wrapper: cdisc_ct staging shards -> Postgres, then
# retire any superseded release's rows. Thin shell over
# `python -m episteme.data.load_articles --source cdisc_ct` followed by
# `python -m episteme.data.cdisc_ct.retire`. run_pipeline.sh forwards
# load_args (--force --reason R when --force was given, else nothing) via
# "$@" to BOTH calls -- retire.py accepts and ignores --force/--reason (see
# its module docstring) so a forced load still runs the retire step. Not
# `exec`'d for the first call: the retire step must run only after
# load_articles has exited 0 (a failed/partial load must never retire the
# release it was trying to replace).
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

"$PY" -m episteme.data.load_articles --source cdisc_ct "$@" || exit $?
exec "$PY" -m episteme.data.cdisc_ct.retire "$@"
