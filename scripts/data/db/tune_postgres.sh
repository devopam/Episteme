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

# Bound every connection attempt: a silent, unreachable server would otherwise
# hang until the OS gives up. Override with PGCONNECT_TIMEOUT (seconds).
export PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-10}"

names_sql() { # 'a','b',... for an IN list (names are the fixed literals above)
    local s out=""
    for s in "${SETTINGS[@]}"; do out+="${out:+,}'${s%%|*}'"; done
    printf '%s' "$out"
}

values_sql() { # ('a','v'),... for a VALUES list
    local s out=""
    for s in "${SETTINGS[@]}"; do out+="${out:+,}('${s%%|*}','${s#*|}')"; done
    printf '%s' "$out"
}

# One row per setting: name|current|pending_restart (t/f). current_setting()
# renders memory and time in the same form as the values above (2GB, 30min).
current_rows() { # current_rows PSQL-INVOCATION...
    "$@" -X -At -F '|' -v ON_ERROR_STOP=1 -c \
        "select name, current_setting(name), pending_restart from pg_settings where name in ($(names_sql)) order by name"
}

# Role- or database-level overrides (ALTER ROLE/DATABASE ... SET) of these
# settings beat the server value for the sessions they match, such as the
# pipeline's episteme_app sessions. One row each: database|role|name=value.
override_rows() { # override_rows PSQL-INVOCATION...
    "$@" -X -At -F '|' -v ON_ERROR_STOP=1 -c \
        "select coalesce(d.datname, '*'), coalesce(r.rolname, '*'), c from pg_db_role_setting s left join pg_database d on d.oid = s.setdatabase left join pg_roles r on r.oid = s.setrole cross join unnest(s.setconfig) c where split_part(c, '=', 1) in ($(names_sql)) order by 1, 2, 3"
}

if [ "$MODE" = "check" ]; then
    require_env EPISTEME_SYS_ADMIN_PASSWORD
    db="${PGDATABASE:-episteme}"
    sa_psql() { PGPASSWORD="$EPISTEME_SYS_ADMIN_PASSWORD" "$PSQL" -h "$PGHOST" -p "$PGPORT" -U episteme_sys_admin -d "$db" "$@"; }
    rows="$(current_rows sa_psql)" || die "tune_postgres.sh: could not read pg_settings on $db"
    overrides="$(override_rows sa_psql)" || die "tune_postgres.sh: could not read pg_db_role_setting on $db"
    differ=0
    printf '%-22s %-10s %-10s %s\n' setting current recommended status
    for s in "${SETTINGS[@]}"; do
        name="${s%%|*}"; want="${s#*|}"
        line="$(printf '%s\n' "$rows" | awk -F'|' -v n="$name" '$1==n')"
        have="$(printf '%s' "$line" | cut -d'|' -f2)"
        pending="$(printf '%s' "$line" | cut -d'|' -f3)"
        status=ok
        if [ "$have" != "$want" ]; then status=DIFFERS; differ=1; fi
        # a pending restart means the next start may bring a different value
        if [ "$pending" = "t" ]; then status="$status (restart pending)"; differ=1; fi
        printf '%-22s %-10s %-10s %s\n' "$name" "${have:-?}" "$want" "$status"
    done
    if [ -n "$overrides" ]; then
        differ=1
        printf '%s\n' "$overrides" | while IFS='|' read -r odb orole ocfg; do
            printf 'OVERRIDE database=%s role=%s %s\n' "$odb" "$orole" "$ocfg"
        done
    fi
    exit "$differ"
fi

# apply
su_user="${POSTGRES_SUPERUSER:-postgres}"
[ -n "${POSTGRES_SUPERUSER_PASSWORD:-}" ] || die "tune_postgres.sh apply: POSTGRES_SUPERUSER_PASSWORD not set (put it in ./.env)"
su_psql() { PGPASSWORD="$POSTGRES_SUPERUSER_PASSWORD" "$PSQL" -h "$PGHOST" -p "$PGPORT" -U "$su_user" -d postgres "$@"; }

# Validate every name and enum value before writing anything: each ALTER SYSTEM
# persists on its own, so a failure halfway would leave part of the set behind.
# Re-running apply converges either way, since every value is a recommended one.
invalid="$(su_psql -X -At -F '|' -v ON_ERROR_STOP=1 -c \
    "select v.name, case when s.name is null then 'unknown setting' when s.vartype = 'enum' and not (v.val = any(s.enumvals)) then 'value not supported by this server' else 'ok' end from (values $(values_sql)) v(name, val) left join pg_settings s on s.name = v.name order by 1" \
    | awk -F'|' '$2 != "ok"')" || die "tune_postgres.sh: could not validate the settings against pg_settings"
[ -z "$invalid" ] || die "tune_postgres.sh: refusing to apply; invalid: $(printf '%s' "$invalid" | paste -sd';' -)"

for s in "${SETTINGS[@]}"; do
    name="${s%%|*}"; want="${s#*|}"
    # ALTER SYSTEM cannot run inside a transaction block: one -c per statement
    su_psql -X -q -v ON_ERROR_STOP=1 -c "ALTER SYSTEM SET $name = '$want'" \
        || die "tune_postgres.sh: ALTER SYSTEM SET $name failed"
    log INFO "set $name = $want"
done
reloaded="$(su_psql -X -At -v ON_ERROR_STOP=1 -c "select pg_reload_conf()")" \
    || die "tune_postgres.sh: pg_reload_conf() failed"
[ "$reloaded" = "t" ] || die "tune_postgres.sh: pg_reload_conf() returned '$reloaded'"

# pg_file_settings re-reads the config files, so it shows a value the server has
# not applied without waiting for the reload to reach any backend.
# pg_settings.pending_restart is unreliable here: on Windows a new backend
# inherits the postmaster's settings and never sets it (seen on PG19beta3).
unapplied="$(su_psql -X -At -v ON_ERROR_STOP=1 -c \
    "select name from pg_file_settings where sourcefile like '%postgresql.auto.conf' and not applied and name in ($(names_sql)) order by name")" \
    || die "tune_postgres.sh: could not read pg_file_settings"

# The reload is asynchronous: re-read the effective values for a few seconds.
# Each setting must end up active, or be waiting for a restart.
tries="${EPISTEME_TUNE_VERIFY_TRIES:-10}"
case "$tries" in ''|*[!0-9]*) tries=10 ;; esac
while :; do
    rows="$(current_rows su_psql)" || die "tune_postgres.sh: could not read back pg_settings"
    inactive=""
    for s in "${SETTINGS[@]}"; do
        name="${s%%|*}"; want="${s#*|}"
        have="$(printf '%s\n' "$rows" | awk -F'|' -v n="$name" '$1==n{print $2}')"
        [ "$have" = "$want" ] && continue
        printf '%s\n' "$unapplied" | grep -qx "$name" && continue
        inactive+="${inactive:+ }$name=${have:-?}"
    done
    [ -z "$inactive" ] && break
    tries=$((tries - 1))
    [ "$tries" -gt 0 ] || die "tune_postgres.sh: not active after reload: $inactive"
    sleep 1
done
if [ -n "$unapplied" ]; then
    log WARN "restart the PostgreSQL service to apply: $(printf '%s' "$unapplied" | paste -sd' ' -)"
else
    log INFO "all settings active"
fi
