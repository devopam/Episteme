#!/usr/bin/env bash
# MeSH embed wrapper: thin shell over `python -m episteme.data.mesh.embed_mesh`, which
# writes the MedCPT article-encoder vectors of the MeSH vocabulary (descriptor name plus
# scope note, and every entry term) into episteme.mesh_term_vec. Run it after the mesh
# graph phase has filled episteme.mesh_term. run_pipeline.sh passes --limit N (a trial)
# and --batch N through "$@".
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

exec "$PY" -m episteme.data.mesh.embed_mesh "$@"
