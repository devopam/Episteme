#!/usr/bin/env bash
# Episteme acquisition wrapper: the English EPAR PDFs that the ema_epar_documents feed lists
# (types from src/episteme/data/regulatory/feeds.toml). Run the `ema` download first: it
# fetches the feed. One file at a time, EPISTEME_EMA_DELAY_SECONDS between requests (default
# 5). A 429 or 503 pauses the run (EPISTEME_EMA_BACKOFF_SECONDS,
# EPISTEME_EMA_MAX_BACKOFF_SECONDS); after EPISTEME_EMA_MAX_THROTTLES in a row the stage stops
# (exit 3) and a re-run resumes. EPISTEME_EMA_READ_TIMEOUT_SECONDS (default 60) bounds a stalled
# read. If EMA answers with its human-verification page the stage stops (exit 3), keeps what it
# fetched, and does not retry; a re-run resumes.
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
[ -n "$PY" ] || die "ema_epar: python not found (set PYTHON=/path/to/python)"

# run_pipeline.sh passes --dry-run as EPISTEME_DRY_RUN=1: turn it into the flag, so a
# dry run never downloads.
[ "${EPISTEME_DRY_RUN:-0}" = "1" ] && set -- --dry-run "$@"

exec "$PY" -m episteme.data.ema.download_epar "$@"
