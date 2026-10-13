#!/usr/bin/env bash
# Recommended PostgreSQL server settings for bulk loads on the local build
# (4 cores, 16 GB RAM, data directory on a USB HDD). Cluster-wide: they apply
# to every database on the server.
#
#   bash scripts/data/db/tune_postgres.sh check   # compare; exit 1 if any differ
#   bash scripts/data/db/tune_postgres.sh apply   # ALTER SYSTEM SET, reload, list restart-pending
#
# check reads pg_settings as episteme_sys_admin (PGDATABASE, default episteme).
# apply needs a superuser: episteme_sys_admin has no ALTER SYSTEM privilege, by
# design. It uses POSTGRES_SUPERUSER / POSTGRES_SUPERUSER_PASSWORD, as
# init_database.sh does, and never echoes a password. Settings marked restart
# take effect only after the service restarts; apply lists them.
#
# fsync, full_page_writes and data checksums are deliberately left on (GxP data
# integrity, docs/11). Sizing and reasons: docs/10 section 2.4.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"

# name|value — keep in step with the table in docs/10 section 2.4
SETTINGS=(
    "shared_buffers|2GB"
    "max_wal_size|16GB"
    "min_wal_size|2GB"
    "checkpoint_timeout|30min"
    "wal_compression|lz4"
    "maintenance_work_mem|1GB"
    "effective_cache_size|8GB"
)

MODE="${1:-check}"
case "$MODE" in
    check|apply) ;;
    -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "tune_postgres.sh: unknown mode '$MODE' (check|apply)" ;;
esac

load_dotenv
require_env PGHOST PGPORT

PSQL="${PSQL:-}"
if [ -z "$PSQL" ]; then
    for c in "/c/Program Files/PostgreSQL/19/bin/psql" "/c/Program Files/PostgreSQL/19/bin/psql.exe" psql; do
        command -v "$c" >/dev/null 2>&1 && { PSQL="$c"; break; }
    done
fi
[ -n "$PSQL" ] || die "psql not found (set PSQL=/path/to/psql)"

names_sql() { # 'a','b',... for an IN list (names are the fixed literals above)
    local s out=""
    for s in "${SETTINGS[@]}"; do out+="${out:+,}'${s%%|*}'"; done
    printf '%s' "$out"
}

# One row per setting: name|current|pending_restart (t/f). current_setting()
# renders memory and time in the same form as the values above (2GB, 30min).
current_rows() { # current_rows PSQL-INVOCATION...
    "$@" -X -At -F '|' -v ON_ERROR_STOP=1 -c \
        "select name, current_setting(name), pending_restart from pg_settings where name in ($(names_sql)) order by name"
}

if [ "$MODE" = "check" ]; then
    require_env EPISTEME_SYS_ADMIN_PASSWORD
    db="${PGDATABASE:-episteme}"
    rows="$(PGPASSWORD="$EPISTEME_SYS_ADMIN_PASSWORD" current_rows "$PSQL" -h "$PGHOST" -p "$PGPORT" \
        -U episteme_sys_admin -d "$db")" || die "tune_postgres.sh: could not read pg_settings on $db"
    differ=0
    printf '%-22s %-10s %-10s %s\n' setting current recommended status
    for s in "${SETTINGS[@]}"; do
        name="${s%%|*}"; want="${s#*|}"
        line="$(printf '%s\n' "$rows" | awk -F'|' -v n="$name" '$1==n')"
        have="$(printf '%s' "$line" | cut -d'|' -f2)"
        pending="$(printf '%s' "$line" | cut -d'|' -f3)"
        status=ok
        if [ "$have" != "$want" ]; then status=DIFFERS; differ=1; fi
        [ "$pending" = "t" ] && status="$status (restart pending)"
        printf '%-22s %-10s %-10s %s\n' "$name" "${have:-?}" "$want" "$status"
    done
    exit "$differ"
fi

# apply
su_user="${POSTGRES_SUPERUSER:-postgres}"
[ -n "${POSTGRES_SUPERUSER_PASSWORD:-}" ] || die "tune_postgres.sh apply: POSTGRES_SUPERUSER_PASSWORD not set (put it in ./.env)"
su_psql() { PGPASSWORD="$POSTGRES_SUPERUSER_PASSWORD" "$PSQL" -h "$PGHOST" -p "$PGPORT" -U "$su_user" -d postgres "$@"; }

for s in "${SETTINGS[@]}"; do
    name="${s%%|*}"; want="${s#*|}"
    # ALTER SYSTEM cannot run inside a transaction block: one -c per statement
    su_psql -X -q -v ON_ERROR_STOP=1 -c "ALTER SYSTEM SET $name = '$want'" \
        || die "tune_postgres.sh: ALTER SYSTEM SET $name failed"
    log INFO "set $name = $want"
done
su_psql -X -q -At -v ON_ERROR_STOP=1 -c "select pg_reload_conf()" >/dev/null \
    || die "tune_postgres.sh: pg_reload_conf() failed"
pending="$(current_rows su_psql | awk -F'|' '$3=="t"{print $1}' | paste -sd' ' -)"
if [ -n "$pending" ]; then
    log WARN "restart the PostgreSQL service to apply: $pending"
else
    log INFO "all settings active"
fi
