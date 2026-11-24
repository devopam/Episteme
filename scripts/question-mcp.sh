#!/usr/bin/env bash
# Launcher for the Episteme question_mcp MCP server (question -> MeSH terms, the person
# confirms the terms by MCP elicitation, then search). Reference it from .mcp.json as
# command: bash, args: [scripts/question-mcp.sh, <db>].
#
# The server connects as episteme_api_reader (password: EPISTEME_API_READER_PASSWORD),
# which may only call the episteme_api functions (grants/0015_api_roles.sql). The password
# lives only in ./.env (gitignored): this script sources ./.env at spawn time and never
# prints a value. episteme.config reads the settings; this script only picks the database.
#
# Usage: question-mcp.sh [episteme|episteme_test]   (default: episteme)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$here/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$here/.env"
  set +a
fi

usage="usage: question-mcp.sh [episteme|episteme_test]"
if [ $# -gt 1 ]; then echo "question-mcp.sh: too many arguments; $usage" >&2; exit 2; fi
db="${1:-episteme}"
case "$db" in
  episteme|episteme_test) ;;
  *) echo "question-mcp.sh: unknown database '$db'; $usage" >&2; exit 2 ;;
esac

: "${EPISTEME_API_READER_PASSWORD:?EPISTEME_API_READER_PASSWORD not set (put it in ./.env)}"
# Fixed here, after ./.env is read: the database named on the command line wins.
export PGDATABASE="$db"
export EPISTEME_DB_TARGET=primary
export PGHOST="${PGHOST:-localhost}"
export PGPORT="${PGPORT:-5433}"

if [ -x "$here/.venv/Scripts/python.exe" ]; then
  py="$here/.venv/Scripts/python.exe"
else
  py="$here/.venv/bin/python"
fi
cd "$here"
exec "$py" -m episteme.reference.question_mcp
