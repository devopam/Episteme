#!/usr/bin/env bash
# Episteme extract wrapper: EMA EPAR PDFs (raw/ema/epar) -> episteme.articles staging parts.
# Thin shell over `python -m episteme.data.ema.extract_epar`. run_pipeline.sh forwards
# --max-files N, --force --reason R (overrides the feed and withdrawal guards, audited) and
# --skip-unit NAME --reason R. --offline-subset runs on the PDFs already on disk (no
# download): the absent documents count as not_downloaded, the guards are unchanged, and the
# run marker records "offline_subset": {"present": n, "listed": m}.
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

exec "$PY" -m episteme.data.ema.extract_epar "$@"
