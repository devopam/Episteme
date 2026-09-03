#!/usr/bin/env bash
# Initialise the Episteme storage core on localhost:5433.
#
# Idempotent / safe to re-run:
#   Phase A (as postgres) — create the episteme_sys_admin + episteme_app roles
#     and the episteme + episteme_test databases only if absent; always reset
#     their passwords from .env.
#   Phase B (as episteme_sys_admin) — apply extensions.sql, then schema.sql to
#     each database. schema.sql itself is not idempotent, so an already-present
#     `episteme` schema is left untouched unless EPISTEME_INIT_FORCE=1, which
#     drops and recreates it (DESTRUCTIVE — data is lost).
#
# Never echoes a password.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"
load_dotenv
require_env PGHOST PGPORT EPISTEME_SYS_ADMIN_PASSWORD EPISTEME_DB_PASSWORD

DB_DIR="$HERE/../../../src/episteme/data/db"
EXT_SQL="$DB_DIR/extensions.sql"
SCHEMA_SQL="$DB_DIR/schema.sql"
[ -f "$EXT_SQL" ]    || die "not found: $EXT_SQL"
[ -f "$SCHEMA_SQL" ] || die "not found: $SCHEMA_SQL"

PSQL="${PSQL:-}"
if [ -z "$PSQL" ]; then
    for c in "/c/Program Files/PostgreSQL/19/bin/psql" "/c/Program Files/PostgreSQL/19/bin/psql.exe" psql; do
        command -v "$c" >/dev/null 2>&1 && { PSQL="$c"; break; }
    done
fi
[ -n "$PSQL" ] || die "psql not found (set PSQL=/path/to/psql)"

SU_USER="${POSTGRES_SUPERUSER:-postgres}"
SU_PW="${POSTGRES_SUPERUSER_PASSWORD:-postgres}"
FORCE="${EPISTEME_INIT_FORCE:-0}"

su_psql() { PGPASSWORD="$SU_PW" "$PSQL" -X -v ON_ERROR_STOP=1 -q \
    -h "$PGHOST" -p "$PGPORT" -U "$SU_USER" "$@"; }
sa_psql() { PGPASSWORD="$EPISTEME_SYS_ADMIN_PASSWORD" "$PSQL" -X -v ON_ERROR_STOP=1 -q \
    -h "$PGHOST" -p "$PGPORT" -U episteme_sys_admin "$@"; }

# ---------------------------------------------------------------------------
log INFO "Phase A: roles + databases (as $SU_USER)"
su_psql -d postgres \
    -v sys_pw="$EPISTEME_SYS_ADMIN_PASSWORD" \
    -v app_pw="$EPISTEME_DB_PASSWORD" <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'episteme_sys_admin') THEN
    CREATE ROLE episteme_sys_admin LOGIN CREATEDB CREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'episteme_app') THEN
    CREATE ROLE episteme_app LOGIN;
  END IF;
END $$;

ALTER ROLE episteme_sys_admin WITH LOGIN CREATEDB CREATEROLE PASSWORD :'sys_pw';
ALTER ROLE episteme_app       WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_pw';

SELECT 'CREATE DATABASE episteme OWNER episteme_sys_admin'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'episteme')\gexec
SELECT 'CREATE DATABASE episteme_test OWNER episteme_sys_admin'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'episteme_test')\gexec

GRANT CONNECT ON DATABASE episteme      TO episteme_app;
GRANT CONNECT ON DATABASE episteme_test TO episteme_app;
SQL

# ---------------------------------------------------------------------------
log INFO "Phase B: extensions + schema (as episteme_sys_admin)"
for db in episteme episteme_test; do
    log INFO "  $db: extensions.sql"
    sa_psql -d "$db" -f "$EXT_SQL"

    has_schema="$(sa_psql -At -d "$db" \
        -c "SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name='episteme')")"
    if [ "$has_schema" = "t" ]; then
        if [ "$FORCE" = "1" ]; then
            log WARN "  $db: episteme schema exists; EPISTEME_INIT_FORCE=1 -> DROP SCHEMA ... CASCADE and recreate"
            sa_psql -d "$db" -c "DROP SCHEMA episteme CASCADE;"
            sa_psql -d "$db" -f "$SCHEMA_SQL"
        else
            log INFO "  $db: episteme schema already present -> skipping schema.sql (EPISTEME_INIT_FORCE=1 to recreate)"
        fi
    else
        log INFO "  $db: schema.sql"
        sa_psql -d "$db" -f "$SCHEMA_SQL"
    fi
done

# ---------------------------------------------------------------------------
log INFO "Summary (episteme)"
sa_psql -At -d episteme <<'SQL'
SELECT 'schema_present      = ' || EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name='episteme')::text;
SELECT 'tables             = ' || count(*)::text FROM pg_tables WHERE schemaname='episteme';
SELECT 'partitioned_parents= ' || count(*)::text
  FROM pg_partitioned_table pt
  JOIN pg_class c ON c.oid = pt.partrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'episteme';
SELECT 'audit: app INSERT  = ' || has_table_privilege('episteme_app','episteme._audit','INSERT')::text;
SELECT 'audit: app SELECT  = ' || has_table_privilege('episteme_app','episteme._audit','SELECT')::text;
SELECT 'audit: app UPDATE  = ' || has_table_privilege('episteme_app','episteme._audit','UPDATE')::text || '  (want false)';
SELECT 'audit: app DELETE  = ' || has_table_privilege('episteme_app','episteme._audit','DELETE')::text || '  (want false)';
SELECT 'sqlpgq graph       = ' ||
  CASE WHEN EXISTS(SELECT 1 FROM pg_catalog.pg_class WHERE relname='episteme_graph')
       THEN 'present' ELSE 'absent (graph_builder uses the CTE path)' END;
SQL

log INFO "init_database.sh: done"
