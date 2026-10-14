#!/usr/bin/env bash
# PubMed knowledge-graph wrapper: the extractor's per-relation graph files under
# <processed>/staging/pubmed_graph -> episteme.article_mesh / article_cites /
# article_comments / article_chemicals / article_keywords / article_authors and
# article_facets, one transaction per source file. Thin shell over `python -m
# episteme.data.graph_builder --source pubmed --processed-dir <processed>`.
# "$@" passes through --force --reason "..." (audited) for direct invocation.
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

exec "$PY" -m episteme.data.graph_builder --source pubmed \
    --raw-dir "${EPISTEME_RAW_ROOT:-./01_raw}/pubmed" \
    --processed-dir "${EPISTEME_PROCESSED_ROOT:-./02_processed}" "$@"
