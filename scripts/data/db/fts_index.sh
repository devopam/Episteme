#!/usr/bin/env bash
# Full-text GIN index builds on article_body, one partition at a time (migration 0015
# creates the functions; the indexes are this operator step). Thin shell over
# `python -m episteme.data.db.fts_index`, which connects as episteme_sys_admin (the
# table owner); the DB-mode guard applies.
#   fts_index.sh status                                        # read-only state per partition
#   fts_index.sh build --partition article_body_X              # checks and plan only
#   fts_index.sh build --partition article_body_X --yes [--data-dir PATH]
# Allowed: article_body_pubmed, _pmc, _openalex, _bookshelf, _dailymed, _ema_epar (exit 2
# for any other). Refused (exit 1) when free space on the index disk is under 2x the
# partition's pg_relation_size. One heavy job at a time; run on the database host; start
# with a small partition (bookshelf or dailymed). Tee the output to D:/episteme/_runlogs/.
# Refuses any server except 19beta3.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"
load_dotenv

case "${1:-}" in
    build|status) ;;
    *) echo "usage: $0 build|status [options]" >&2; exit 2 ;;
esac
require_env EPISTEME_ACTOR EPISTEME_SYS_ADMIN_PASSWORD

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in "$HERE/../../../.venv/Scripts/python.exe" "$HERE/../../../.venv/bin/python" python; do
        command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
    done
fi
[ -n "$PY" ] || die "python not found (set PYTHON=/path/to/python)"

"$PY" -m episteme.data.db.fts_index "$@"
exit $?
