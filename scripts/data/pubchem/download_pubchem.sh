#!/usr/bin/env bash
# Episteme SP3 acquisition wrapper: PubChem bulk download. Thin shell over
# scripts/data/_lib/common.sh — directory listing + size-skip + fetch only.
# MODE positional: compound_extras (default) | rdf_compound | compound_full | all_nlp.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"

MODE="compound_extras"
MAX_FILES=""
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)   export EPISTEME_DRY_RUN=1; shift ;;
        --max-files) MAX_FILES="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --force)     FORCE=1; shift ;;
        --reason)    shift; [ $# -gt 0 ] && shift ;;   # run_pipeline already enforced it
        compound_extras|rdf_compound|compound_full|all_nlp) MODE="$1"; shift ;;
        *)           log WARN "download_pubchem.sh: ignoring $1"; shift ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR PUBCHEM_BASE

dest="$(resolve_dest pubchem)"

# MODE -> PubChem relative-directory set (from the batch script).
case "$MODE" in
    compound_extras|all_nlp) rels=("Compound/Extras") ;;
    rdf_compound)            rels=("RDF/compound") ;;
    compound_full)           rels=("Compound/CURRENT-Full" "Compound/Extras") ;;
    *)                       die "pubchem: bad mode '$MODE'" ;;
esac

planned=()
for rel in "${rels[@]}"; do
    # No extension filter (take everything the listing yields); '[^/]$' drops
    # trailing-slash sub-directory entries the scraper does not strip.
    mapfile -t fs < <(list_manifest "$PUBCHEM_BASE/$rel" '[^/]$')
    for f in "${fs[@]}"; do
        [ -n "$f" ] || continue
        planned+=("$PUBCHEM_BASE/$rel/$f"$'\t'"$rel/$f")
    done
done
[ "${#planned[@]}" -gt 0 ] || die "pubchem: no files resolved under $PUBCHEM_BASE (mode=$MODE)"

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
    log INFO "pubchem: up to date"
    write_sync_stamp "$dest"
    exit 0
fi

if printf '%s\n' "${lines[@]}" | http_fetch "$dest"; then
    write_sync_stamp "$dest"
else
    die "pubchem: fetch failed"
fi
