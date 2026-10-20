#!/usr/bin/env bash
# Bulk-load mode: drop a group of secondary indexes (articles, or the
# pubmed_graph tables) before a large load, rebuild them after, or show a
# pending rebuild. Thin shell over
# `python -m episteme.data.db.bulk_indexes`, which connects as
# episteme_sys_admin (the table owner); the DB-mode guard applies.
#   bulk_load_indexes.sh drop [--group articles|pubmed_graph]   # default articles; exit 2 if a previous rebuild is pending
#   bulk_load_indexes.sh rebuild   # every pending index, any group; idempotent; safe to re-run after a failure
#   bulk_load_indexes.sh status    # exit 1 while a rebuild is pending, 3 on error
# A missing EPISTEME_ACTOR / EPISTEME_SYS_ADMIN_PASSWORD exits 2 (require_env),
# for status too.
# run_pipeline.sh <source> load --bulk (group articles) and graph --bulk (group
# pubmed_graph) run drop and rebuild around the stage.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"
load_dotenv

case "${1:-}" in
    drop|rebuild|status) ;;
    *) echo "usage: $0 drop|rebuild|status [--group articles|pubmed_graph]" >&2; exit 2 ;;
esac
ACTION="$1"; shift
require_env EPISTEME_ACTOR EPISTEME_SYS_ADMIN_PASSWORD

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in "$HERE/../../../.venv/Scripts/python.exe" "$HERE/../../../.venv/bin/python" python; do
        command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
    done
fi
[ -n "$PY" ] || die "python not found (set PYTHON=/path/to/python)"

# drop and rebuild take the heavy lock (status only reads). Under
# run_pipeline.sh the call re-enters the runner's lock (same run id and runner
# PID), so this script releases only a lock it acquired itself.
if [ "$ACTION" != "status" ]; then
    LOCK_RUN_ID="${EPISTEME_RUN_ID:-adhoc-$EPISTEME_RUNNER_PID}"
    lock_rc=0
    lock_out="$("$PY" -m episteme.ops.lock acquire heavy --run-id "$LOCK_RUN_ID" \
        --stage bulk_load_indexes --owner-pid "$EPISTEME_RUNNER_PID")" || lock_rc=$?
    [ "$lock_rc" -eq 0 ] || exit "$lock_rc"
    log INFO "bulk_load_indexes: heavy lock: $lock_out"
    # Under run_pipeline.sh the acquire re-enters the runner's lock, so no trap
    # is set here: a Ctrl-C follows the runner's handling, and the runner
    # releases its own lock.
    if [ "$lock_out" = "acquired" ]; then
        # shellcheck disable=SC2329 # called by the EXIT trap
        _release_lock() {
            "$PY" -m episteme.ops.lock release heavy --run-id "$LOCK_RUN_ID" \
                --owner-pid "$EPISTEME_RUNNER_PID" >/dev/null 2>&1 \
                || log WARN "bulk_load_indexes: could not release the heavy lock"
        }
        trap _release_lock EXIT
        trap 'exit 130' INT
        trap 'exit 143' TERM
    fi
fi

"$PY" -m episteme.data.db.bulk_indexes "$ACTION" "$@"
exit $?
