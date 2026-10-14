#!/usr/bin/env bash
# Launcher for the MCPg MCP server against the Episteme PG19 database.
# Referenced by .mcp.json (command: bash, args: [scripts/mcpg-episteme.sh, <primary-db> [--admin]]).
#
# Why a launcher instead of plain ${VAR} expansion in .mcp.json: the DB
# password lives only in ./.env (gitignored). This sources ./.env at spawn
# time so no connection string is committed and no "export before you run
# claude" ritual is needed. Values in ./.env override the inherited environment.
#
# Modes:
#   <db>           everyday server: connects as episteme_sys_admin. MCPg's ALTER
#                  SYSTEM tools are always off, whatever ./.env says.
#   <db> --admin   admin server: connects as POSTGRES_SUPERUSER (default
#                  postgres) so MCPg's alter_system / reload_config tools work.
#                  Every MCPg tool then runs as the superuser, so keep this
#                  server disabled in /mcp except while changing server settings.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$here/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$here/.env"
  set +a
fi

usage="usage: mcpg-episteme.sh [episteme|episteme_test] [--admin]"
if [ $# -gt 2 ]; then echo "mcpg-episteme.sh: too many arguments; $usage" >&2; exit 2; fi
primary="${1:-episteme}"                     # episteme | episteme_test
mode="${2:-}"
case "$primary" in
  -*|"") echo "mcpg-episteme.sh: expected a database name first, got '$primary'; $usage" >&2; exit 2 ;;
esac
case "$mode" in
  ""|--admin) ;;
  *) echo "mcpg-episteme.sh: unknown option '$mode'; $usage" >&2; exit 2 ;;
esac
host="${PGHOST:-localhost}"
port="${PGPORT:-5433}"

# Percent-encode a URI userinfo part, so any character in a password is safe.
urlenc() {
  local s="$1" out="" c i
  local LC_ALL=C
  for ((i = 0; i < ${#s}; i++)); do
    c="${s:i:1}"
    case "$c" in
      [A-Za-z0-9._~-]) out+="$c" ;;
      *) printf -v c '%%%02X' "'$c"; out+="$c" ;;
    esac
  done
  printf '%s' "$out"
}

if [ "$mode" = "--admin" ]; then
  user="${POSTGRES_SUPERUSER:-postgres}"
  pw="${POSTGRES_SUPERUSER_PASSWORD:?POSTGRES_SUPERUSER_PASSWORD not set (put it in ./.env)}"
  user_enc="$(urlenc "$user")"
  pw_enc="$(urlenc "$pw")"
  export MCPG_DATABASE_URL="postgresql://${user_enc}:${pw_enc}@${host}:${port}/${primary}"
  unset MCPG_SECONDARY_DATABASE_URLS
  export MCPG_ALLOW_ALTER_SYSTEM=true
  export MCPG_ACCESS_MODE=unrestricted         # MCPg registers the ALTER SYSTEM tools only in this mode
  # The server settings docs/10 section 2.4 manages; MCPg's built-in denylist still applies.
  export MCPG_ALTER_SYSTEM_ALLOWLIST="${MCPG_ALTER_SYSTEM_ALLOWLIST:-shared_buffers,max_wal_size,min_wal_size,checkpoint_timeout,wal_compression,maintenance_work_mem,effective_cache_size,max_parallel_maintenance_workers}"
else
  pw="${EPISTEME_SYS_ADMIN_PASSWORD:?EPISTEME_SYS_ADMIN_PASSWORD not set (put it in ./.env)}"
  pw_enc="$(urlenc "$pw")"
  export MCPG_DATABASE_URL="postgresql://episteme_sys_admin:${pw_enc}@${host}:${port}/${primary}"
  # Only the episteme-primary server gets episteme_test as a (read-only) secondary;
  # write/DDL to episteme_test needs the mcpg-test server whose primary IS episteme_test.
  if [ "$primary" = "episteme" ]; then
    export MCPG_SECONDARY_DATABASE_URLS="episteme_test=postgresql://episteme_sys_admin:${pw_enc}@${host}:${port}/episteme_test"
  fi
  export MCPG_ALLOW_ALTER_SYSTEM=false
fi
export MCPG_ACCESS_MODE="${MCPG_ACCESS_MODE:-unrestricted}"
export MCPG_ALLOW_DDL="${MCPG_ALLOW_DDL:-true}"
export MCPG_TRANSPORT=stdio

exec mcpg
