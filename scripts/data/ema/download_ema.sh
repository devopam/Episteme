#!/usr/bin/env bash
# Episteme acquisition wrapper: EMA open-data feeds (JSON reports + the Article 57
# workbook). Download-only. The feed URLs come from the EMA_* keys of
# scripts/data/_lib/sources.env, named by src/episteme/data/regulatory/feeds.toml.
# One file at a time, EPISTEME_EMA_DELAY_SECONDS between requests (default 2). If EMA
# answers with its human-verification page the stage stops (exit 3) and does not retry.
# Flags: --dry-run, --force, --max-files N, --reason TEXT, --output-dir DIR.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"
load_dotenv
require_env EPISTEME_ACTOR

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in "$HERE/../../../.venv/Scripts/python.exe" "$HERE/../../../.venv/bin/python" python; do
        command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
    done
fi
[ -n "$PY" ] || die "ema: python not found (set PYTHON=/path/to/python)"

# run_pipeline.sh passes --dry-run as EPISTEME_DRY_RUN=1: turn it into the flag, so a
# dry run never downloads.
[ "${EPISTEME_DRY_RUN:-0}" = "1" ] && set -- --dry-run "$@"

exec "$PY" -m episteme.data.regulatory.download_ema "$@"
