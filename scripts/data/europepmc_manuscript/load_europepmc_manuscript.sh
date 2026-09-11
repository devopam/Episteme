#!/usr/bin/env bash
# SP2 literature-load wrapper: europepmc_manuscript staging shards ->
# episteme.articles + episteme.article_body. Thin shell over `python -m
# episteme.data.load_articles --source europepmc_manuscript`. run_pipeline.sh
# forwards load_args (--force --reason R when --force was given, else nothing).
#
# NOTE ON LOCATION: see extract_europepmc_manuscript.sh in this same directory
# — run_pipeline.sh's extract/load/graph dispatch requires this flat
# scripts/data/europepmc_manuscript/ path (literal $SOURCE), not the nested
# scripts/data/europepmc/manuscripts/ the download wrapper and python package
# use.
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

exec "$PY" -m episteme.data.load_articles --source europepmc_manuscript "$@"
