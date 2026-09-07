#!/usr/bin/env bash
# Episteme SP3 acquisition wrapper: NLM MeSH bulk download. Thin shell over
# scripts/data/_lib/common.sh — directory listing + size-skip + fetch only.
# Optional positional YEAR (default: current UTC year). Probes several known NLM
# layouts; every one that resolves contributes files.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"

YEAR="$(date -u +%Y)"
MAX_FILES=""
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)   export EPISTEME_DRY_RUN=1; shift ;;
        --max-files) MAX_FILES="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --force)     FORCE=1; shift ;;
        --reason)    shift; [ $# -gt 0 ] && shift ;;   # run_pipeline already enforced it
        [0-9]*)      YEAR="$1"; shift ;;
        *)           log WARN "download_mesh.sh: ignoring $1"; shift ;;
    esac
done

case "$YEAR" in
    [0-9][0-9][0-9][0-9]) ;;
    *) log WARN "download_mesh.sh: YEAR '$YEAR' is not 4 digits — using current year"
       YEAR="$(date -u +%Y)" ;;
esac
PREV=$((YEAR - 1))

load_dotenv
require_env EPISTEME_ACTOR MESH_BASE MESH_FTP_BASE

dest="$(resolve_dest mesh)"

# Candidate directories, tried in order; every one that lists contributes.
cand_dirs=(
    "$MESH_BASE/xmlmesh$YEAR"
    "$MESH_BASE/ascii$YEAR"
    "$MESH_BASE/xmlmesh$PREV"
    "$MESH_BASE/ascii$PREV"
    "$MESH_FTP_BASE/mesh_data"
    "$MESH_FTP_BASE/rdf"
)

planned=()
for d in "${cand_dirs[@]}"; do
    seg="${d##*/}"
    mapfile -t fs < <(list_manifest "$d" '\.(bin|xml|gz|txt|nt|rdf|asc)$')
    for f in "${fs[@]}"; do
        [ -n "$f" ] || continue
        planned+=("$d/$f"$'\t'"$seg/$f")
    done
done

# Fallback: nothing listed -> probe known descriptor filenames with a bare
# curl http-code check (W-3 allows this). Runs under --dry-run too (cheap).
if [ "${#planned[@]}" -eq 0 ]; then
    log WARN "mesh: directory listings resolved nothing — probing known filenames"
    for y in "$YEAR" "$PREV"; do
        for f in "desc$y.xml" "qual$y.xml" "supp$y.xml"; do
            # No -L: a stale path that 302-redirects to an error page (current
            # NLM behaviour) must read as non-200 and be dropped, not chased to
            # a 200 error body that would then be fetched and falsely stamped.
            code="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 15 --max-time 30 "$MESH_BASE/xmlmesh$y/$f" || true)"
            [ "$code" = "200" ] && planned+=("$MESH_BASE/xmlmesh$y/$f"$'\t'"xmlmesh$y/$f")
        done
    done
fi

# Still nothing: a layout change must NOT fail the Task 11 dry-run sweep.
if [ "${#planned[@]}" -eq 0 ]; then
    log WARN "mesh: no files resolved — NLM layout may have changed (check $MESH_BASE); resolved 0 files"
    exit 0
fi

lines=()
for entry in "${planned[@]}"; do
    url="${entry%%$'\t'*}"
    rel="${entry#*$'\t'}"
    # W-4: force a re-fetch — but never delete real files during a --dry-run preview
    if [ "$FORCE" = "1" ] && [ "${EPISTEME_DRY_RUN:-0}" != "1" ]; then rm -f "$dest/$rel"; fi
    if size_match_skip "$dest/$rel" "$url"; then
        log INFO "skip (size-matched): $rel"
    else
        lines+=("$entry")
    fi
done

if [ "${#lines[@]}" -eq 0 ]; then
    log INFO "mesh: up to date"
    write_sync_stamp "$dest"
    exit 0
fi

if printf '%s\n' "${lines[@]}" | cap_urls "$MAX_FILES" | http_fetch "$dest"; then
    write_sync_stamp "$dest"
else
    die "mesh: fetch failed"
fi
