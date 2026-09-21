#!/usr/bin/env bash
# Episteme SP3 acquisition wrapper: NLM MeSH bulk download. Thin shell over
# scripts/data/_lib/common.sh — descriptor-release probe + size-skip + fetch only.
# Optional positional YEAR (default: current UTC year). Resolves the current (else previous) year's descriptor release.
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
require_env EPISTEME_ACTOR MESH_BASE

dest="$(resolve_dest mesh)"

# Current NLM layout: one gzipped DescriptorRecordSet per release under
# $MESH_BASE/MESH_FILES/xmlmesh/desc<year>.gz. Only descriptors are fetched —
# qualifier/supplemental/pharmacologic files are consumed by no serializer.
# Probe the current year, then the previous; the first that returns 200 wins.
# No -L on the probe: a stale path 302-redirects to an HTML error page (current
# NLM behaviour) that would read as 200 if chased, then be fetched and falsely
# stamped. Headers only (-I) — never a body download. Runs under --dry-run too.
planned=()
tried=()
for y in "$YEAR" "$PREV"; do
    for f in "desc$y.gz" "desc$y.xml"; do
        url="$MESH_BASE/MESH_FILES/xmlmesh/$f"
        code="$(curl -sSI -o /dev/null -w '%{http_code}' --connect-timeout 15 --max-time 30 "$url" || true)"
        tried+=("$f=${code:-000}")
        if [ "$code" = "200" ]; then
            planned+=("$url"$'\t'"$f")   # flat dest: 01_raw/mesh/desc<year>.gz
            break
        fi
    done
    [ "${#planned[@]}" -gt 0 ] && break
done

# Nothing resolved: a layout change must NOT fail the dry-run sweep.
if [ "${#planned[@]}" -eq 0 ]; then
    log WARN "mesh: probed $MESH_BASE/MESH_FILES/xmlmesh/ (${tried[*]}) — none returned HTTP 200; NLM layout may have changed (check $MESH_BASE); resolved 0 files"
    exit 0
fi

# C1: --max-files caps the RESOLVED set here, before the --force prune loop —
# otherwise --force deletes the whole set and only N are re-fetched.
if [ -n "$MAX_FILES" ]; then
    mapfile -t planned < <(printf '%s\n' "${planned[@]}" | cap_urls "$MAX_FILES")
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

if printf '%s\n' "${lines[@]}" | http_fetch "$dest"; then
    write_sync_stamp "$dest"
else
    die "mesh: fetch failed"
fi
