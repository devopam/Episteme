#!/usr/bin/env bash
# Apply pending numbered migrations from src/episteme/data/db/migrations/*.sql
# to the target database (default: $PGDATABASE or episteme), as episteme_sys_admin.
# Applied migrations are tracked in episteme._migrations(name, applied_at).
#
#   bash scripts/data/db/migrate_database.sh [target_db]
#
# Note: 0001_add_chunk_vector_columns.sql needs pgvector installed; it will
# fail (and stop the run) on a server where `vector` is absent — that is the
# documented Phase-1 dependency, not a bug.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"
load_dotenv
require_env PGHOST PGPORT EPISTEME_SYS_ADMIN_PASSWORD

MIG_DIR="$HERE/../../../src/episteme/data/db/migrations"
[ -d "$MIG_DIR" ] || die "not found: $MIG_DIR"
TARGET_DB="${1:-${PGDATABASE:-episteme}}"

PSQL="${PSQL:-}"
if [ -z "$PSQL" ]; then
    for c in "/c/Program Files/PostgreSQL/19/bin/psql" "/c/Program Files/PostgreSQL/19/bin/psql.exe" psql; do
        command -v "$c" >/dev/null 2>&1 && { PSQL="$c"; break; }
    done
fi
[ -n "$PSQL" ] || die "psql not found (set PSQL=/path/to/psql)"

sa_psql() { PGPASSWORD="$EPISTEME_SYS_ADMIN_PASSWORD" "$PSQL" -X -v ON_ERROR_STOP=1 -q \
    -h "$PGHOST" -p "$PGPORT" -U episteme_sys_admin -d "$TARGET_DB" "$@"; }

log INFO "migrate_database.sh: target=$TARGET_DB"
sa_psql -c "CREATE SCHEMA IF NOT EXISTS episteme;" || die "CREATE SCHEMA episteme failed"
sa_psql -c "CREATE TABLE IF NOT EXISTS episteme._migrations (
              name       text PRIMARY KEY,
              applied_at timestamptz NOT NULL DEFAULT now()
            );" || die "CREATE TABLE episteme._migrations failed"

applied=0
shopt -s nullglob
mapfile -t files < <(printf '%s\n' "$MIG_DIR"/*.sql | sort)
# printf on a zero-match nullglob expansion still emits one empty line, so
# `files` can hold a single "" entry even when nothing matched -- detect that
# case explicitly rather than trusting `${#files[@]}` alone.
[ ${#files[@]} -eq 0 ] || [ ! -f "${files[0]}" ] && { log INFO "no migrations to apply"; exit 0; }
for f in "${files[@]}"; do
    name="$(basename "$f")"
    if [ "$(sa_psql -At -c "SELECT 1 FROM episteme._migrations WHERE name = '${name//\'/\'\'}'")" = "1" ]; then
        log INFO "  skip (already applied): $name"
        continue
    fi
    log INFO "  applying: $name"
    sa_psql -f "$f" || die "migration failed: $name"
    sa_psql -c "INSERT INTO episteme._migrations (name) VALUES ('${name//\'/\'\'}');"
    applied=$((applied + 1))
done

log INFO "migrate_database.sh: $applied migration(s) applied to $TARGET_DB"
