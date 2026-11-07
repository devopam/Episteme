#!/usr/bin/env bash
# Regulatory reference-feed load wrapper: ema raw files -> episteme.reg_record, one
# partition per release; the latest release moves only after the checks pass.
# Thin shell over `python -m episteme.data.regulatory.load_reg --source ema`.
# run_pipeline.sh forwards --force --reason R when --force was given.
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

exec "$PY" -m episteme.data.regulatory.load_reg --source ema "$@"
