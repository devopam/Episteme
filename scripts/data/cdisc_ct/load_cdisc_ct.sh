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
#
# I1 (whole-branch review, Ruling FW7-1): load_articles' own shard discovery
# globs the WHOLE staging directory, which can hold an older release's shard
# left over from before this run's newest release was discovered/serialized
# -- loading it (even transiently, e.g. under --force or after a retry)
# would let an older release's stable ids briefly overwrite the newer
# release's rows via postgres_loader's (d0) id-collision guard, only for the
# retire step right after to delete the rest of that older release anyway.
# `retire.py --print-current-shards` prints exactly one shard file name per
# package -- the one belonging to its newest LOCALLY DISCOVERED,
# ALREADY-SERIALIZED release -- and those names are forwarded to
# load_articles as repeated `--only NAME`, so nothing else in the staging
# directory is ever touched by this run.
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

only_args=()
while IFS= read -r shard; do
    [ -n "$shard" ] && only_args+=(--only "$shard")
done < <("$PY" -m episteme.data.cdisc_ct.retire --print-current-shards)

if [ "${#only_args[@]}" -eq 0 ]; then
    echo "cdisc_ct: no current (locally discovered + serialized) shards to load"
else
    "$PY" -m episteme.data.load_articles --source cdisc_ct "${only_args[@]}" "$@" || exit $?
fi

exec "$PY" -m episteme.data.cdisc_ct.retire "$@"
