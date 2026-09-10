#!/usr/bin/env bash
# SP2 literature-extract wrapper: ApolloCorpus JSON/JSONL docs -> staging shards.
# Thin shell over `python -m episteme.data.apollo.extract_apollo`. Accepts
# only --max-files N and --force (forwarded via "$@"); run_pipeline.sh builds
# that arg list (lit_extract_args) — NOT the SP3 download wrapper_args superset.
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
[ -n "$PY" ] || die "python not found (set PYTHON=/path/to/python)"

exec "$PY" -m episteme.data.apollo.extract_apollo "$@"
