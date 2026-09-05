#!/usr/bin/env bash
# Launcher for the MCPg MCP server against the Episteme PG19 database.
# Referenced by .mcp.json (command: bash, args: [scripts/mcpg-episteme.sh, <primary-db>]).
#
# Why a launcher instead of plain ${VAR} expansion in .mcp.json: the DB
# password lives only in ./.env (gitignored). This sources ./.env at spawn
# time so no connection string is committed and no "export before you run
# claude" ritual is needed. Real environment variables still win over .env.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$here/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$here/.env"
  set +a
fi

primary="${1:-episteme}"                     # episteme | episteme_test
pw="${EPISTEME_SYS_ADMIN_PASSWORD:?EPISTEME_SYS_ADMIN_PASSWORD not set (put it in ./.env)}"
host="${PGHOST:-localhost}"
port="${PGPORT:-5433}"

export MCPG_DATABASE_URL="postgresql://episteme_sys_admin:${pw}@${host}:${port}/${primary}"
# Only the episteme-primary server gets episteme_test as a (read-only) secondary;
# write/DDL to episteme_test needs the mcpg-test server whose primary IS episteme_test.
if [ "$primary" = "episteme" ]; then
  export MCPG_SECONDARY_DATABASE_URLS="episteme_test=postgresql://episteme_sys_admin:${pw}@${host}:${port}/episteme_test"
fi
export MCPG_ACCESS_MODE="${MCPG_ACCESS_MODE:-unrestricted}"
export MCPG_ALLOW_DDL="${MCPG_ALLOW_DDL:-true}"
export MCPG_TRANSPORT=stdio

exec mcpg
