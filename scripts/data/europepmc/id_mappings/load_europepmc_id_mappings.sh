#!/usr/bin/env bash
# SP2 literature-load wrapper (europepmc_id_mappings exception): Europe PMC
# id_mappings CSV -> episteme.id_map. Thin shell over `python -m
# episteme.data.europepmc.id_mappings.load_id_mappings`. run_pipeline.sh
# forwards load_args (--force --reason R when --force was given, else
# nothing -- --reason is accepted/ignored downstream, matching the other SP2
# load wrappers; the id_map loader's own --force is a documented no-op).
#
# NOTE ON LOCATION: unlike the six SP2 literature sources (flat
# scripts/data/<source>/load_<source>.sh), europepmc_id_mappings is NOT in
# LIT_SOURCES -- run_pipeline.sh special-cases it to this nested path (same
# depth as download_europepmc_id_mappings.sh in this same directory) so the
# download wrapper keeps working unchanged. See run_pipeline.sh's `load)` arms
# (early-validation block + dispatch block) for the two-arm exception.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../_lib/common.sh
. "$HERE/../../_lib/common.sh"
load_dotenv
require_env EPISTEME_ACTOR

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for c in "$HERE/../../../../.venv/Scripts/python.exe" "$HERE/../../../../.venv/bin/python" python; do
        command -v "$c" >/dev/null 2>&1 && { PY="$c"; break; }
    done
fi
[ -n "$PY" ] || die "python not found (set PYTHON=/path/to/python)"

exec "$PY" -m episteme.data.europepmc.id_mappings.load_id_mappings "$@"
