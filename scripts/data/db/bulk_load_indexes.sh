#!/usr/bin/env bash
# Bulk-load mode: drop the secondary indexes of episteme.articles before a
# large load, rebuild them after, or show a pending rebuild. Thin shell over
# `python -m episteme.data.db.bulk_indexes`, which connects as
# episteme_sys_admin (the table owner); the DB-mode guard applies.
#   bulk_load_indexes.sh drop      # exit 2 if a previous rebuild is pending
#   bulk_load_indexes.sh rebuild   # idempotent; safe to re-run after a failure
#   bulk_load_indexes.sh status    # exit 1 while a rebuild is pending, 3 on error
# run_pipeline.sh <source> load --bulk runs drop and rebuild around the load.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"
load_dotenv

case "${1:-}" in
    drop|rebuild|status) ;;
    *) echo "usage: $0 drop|rebuild|status" >&2; exit 2 ;;
esac
require_env EPISTEME_ACTOR EPISTEME_SYS_ADMIN_PASSWORD

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in "$HERE/../../../.venv/Scripts/python.exe" "$HERE/../../../.venv/bin/python" python; do
        command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
    done
fi
[ -n "$PY" ] || die "python not found (set PYTHON=/path/to/python)"

exec "$PY" -m episteme.data.db.bulk_indexes "$1"
