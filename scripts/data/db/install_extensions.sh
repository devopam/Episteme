#!/usr/bin/env bash
# Best-effort install of pgvector (`vector`) + `pg_search` into episteme and
# episteme_test. CREATE EXTENSION needs a superuser, so this connects as the
# cluster superuser (postgres). On the PG19beta3 / Windows build the extension
# files are absent — that is a SOFT block: the exact server error is printed,
# the deferral to migrations/0001 is noted, and the script still exits 0.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"
load_dotenv
require_env PGHOST PGPORT

# Prefer the v19 client if it is installed; fall back to whatever `psql` is on PATH.
PSQL="${PSQL:-}"
if [ -z "$PSQL" ]; then
    for c in "/c/Program Files/PostgreSQL/19/bin/psql" "/c/Program Files/PostgreSQL/19/bin/psql.exe" psql; do
        command -v "$c" >/dev/null 2>&1 && { PSQL="$c"; break; }
    done
fi
[ -n "$PSQL" ] || die "psql not found (set PSQL=/path/to/psql)"

SU_USER="${POSTGRES_SUPERUSER:-postgres}"
SU_PW="${POSTGRES_SUPERUSER_PASSWORD:-postgres}"

for db in episteme episteme_test; do
    log INFO "install_extensions: $db (as $SU_USER)"
    PGPASSWORD="$SU_PW" "$PSQL" -X -v ON_ERROR_STOP=0 -q \
        -h "$PGHOST" -p "$PGPORT" -U "$SU_USER" -d "$db" <<'SQL'
DO $$ BEGIN
  CREATE EXTENSION IF NOT EXISTS vector;
  RAISE NOTICE 'vector: installed';
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'vector: NOT installed -> % -- chunks.embedding deferred to migrations/0001', SQLERRM;
END $$;
DO $$ BEGIN
  CREATE EXTENSION IF NOT EXISTS pg_search;
  RAISE NOTICE 'pg_search: installed';
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'pg_search: NOT installed -> % -- BM25 indexes deferred to migrations/0001', SQLERRM;
END $$;
SQL
done

log INFO "install_extensions: done (a missing extension file on 19beta3 is expected and non-fatal)"
exit 0
