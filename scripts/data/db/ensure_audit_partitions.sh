#!/usr/bin/env bash
# Ensure rolling monthly episteme._audit partitions exist for the next N
# calendar months (default 6), starting from the current month, as
# episteme_sys_admin. Idempotent: CREATE TABLE IF NOT EXISTS makes partition
# creation safe to re-run, and the REVOKE below is re-applied on every
# partition this script touches (not just newly created ones) because it is
# cheap and safe to repeat.
#
# Why re-apply REVOKE at all: schema.sql grants episteme_app full DML on
# ALL TABLES IN SCHEMA episteme (including UPDATE/DELETE), then revokes
# UPDATE/DELETE explicitly on episteme._audit and its partitions so the
# audit trail stays append-only for the app role. ALTER DEFAULT PRIVILEGES
# only affects GRANTs made after it runs -- it cannot pre-emptively revoke
# anything from a table that doesn't exist yet -- so every partition this
# script creates inherits the broad GRANT and needs its own REVOKE, exactly
# like the three partitions schema.sql creates statically (see its
# "Ownership + grants" section).
#
#   bash scripts/data/db/ensure_audit_partitions.sh [target_db] [months_ahead]
#
# target_db    default: ${PGDATABASE:-episteme}
# months_ahead default: 6
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"
load_dotenv
require_env PGHOST PGPORT EPISTEME_SYS_ADMIN_PASSWORD

TARGET_DB="${1:-${PGDATABASE:-episteme}}"
MONTHS_AHEAD="${2:-6}"
case "$MONTHS_AHEAD" in
    ''|*[!0-9]*) die "months_ahead must be a positive integer (got '$MONTHS_AHEAD')" ;;
esac
[ "$MONTHS_AHEAD" -gt 0 ] || die "months_ahead must be a positive integer (got '$MONTHS_AHEAD')"

PSQL="${PSQL:-}"
if [ -z "$PSQL" ]; then
    for c in "/c/Program Files/PostgreSQL/19/bin/psql" "/c/Program Files/PostgreSQL/19/bin/psql.exe" psql; do
        command -v "$c" >/dev/null 2>&1 && { PSQL="$c"; break; }
    done
fi
[ -n "$PSQL" ] || die "psql not found (set PSQL=/path/to/psql)"

sa_psql() { PGPASSWORD="$EPISTEME_SYS_ADMIN_PASSWORD" "$PSQL" -X -v ON_ERROR_STOP=1 -q \
    -h "$PGHOST" -p "$PGPORT" -U episteme_sys_admin -d "$TARGET_DB" "$@"; }

log INFO "ensure_audit_partitions.sh: target=$TARGET_DB months_ahead=$MONTHS_AHEAD"

this_month="$(date -u +%Y-%m-01)"
i=0
while [ "$i" -lt "$MONTHS_AHEAD" ]; do
    start="$(date -u -d "$this_month +$i month" +%Y-%m-%d)" || die "date arithmetic failed for month offset $i"
    end="$(date -u -d "$this_month +$((i + 1)) month" +%Y-%m-%d)" || die "date arithmetic failed for month offset $((i + 1))"
    suffix="$(date -u -d "$start" +%Y%m)" || die "date formatting failed for $start"
    part="_audit_${suffix}"

    sa_psql -c "CREATE TABLE IF NOT EXISTS episteme.${part} PARTITION OF episteme._audit
                  FOR VALUES FROM ('${start}') TO ('${end}');" \
        || die "CREATE TABLE episteme.${part} failed"
    sa_psql -c "REVOKE UPDATE, DELETE ON episteme.${part} FROM episteme_app;" \
        || die "REVOKE on episteme.${part} failed"

    log INFO "ensure_audit_partitions: ensured episteme.${part}"
    i=$((i + 1))
done

log INFO "ensure_audit_partitions.sh: $MONTHS_AHEAD partition(s) ensured on $TARGET_DB"
