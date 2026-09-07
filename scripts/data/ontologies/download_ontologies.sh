#!/usr/bin/env bash
# Episteme SP3 acquisition wrapper: open ontologies bulk download (GO, HPO,
# MONDO, UCUM). Thin shell over scripts/data/_lib/common.sh — fixed item list +
# size-skip + fetch only. No discovery, no MODE.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"

MAX_FILES=""
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)   export EPISTEME_DRY_RUN=1; shift ;;
        --max-files) MAX_FILES="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --force)     FORCE=1; shift ;;
        --reason)    shift; [ $# -gt 0 ] && shift ;;   # run_pipeline already enforced it
        *)           log WARN "download_ontologies.sh: ignoring $1"; shift ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR OBO_PURL_BASE UCUM_ESSENCE_URL UCUM_README_URL

dest="$(resolve_dest ontologies)"

# Fixed item set (from the batch script's ITEMS): sub|filename|url.
items=(
    "go|go.obo|$OBO_PURL_BASE/go.obo"
    "go|go.owl|$OBO_PURL_BASE/go.owl"
    "hpo|hp.obo|$OBO_PURL_BASE/hp.obo"
    "hpo|hp.owl|$OBO_PURL_BASE/hp.owl"
    "mondo|mondo.obo|$OBO_PURL_BASE/mondo.obo"
    "mondo|mondo.owl|$OBO_PURL_BASE/mondo.owl"
    "ucum|ucum-essence.xml|$UCUM_ESSENCE_URL"
    "ucum|README.md|$UCUM_README_URL"
)

lines=()
for it in "${items[@]}"; do
    IFS='|' read -r sub fname url <<< "$it"
    rel="$sub/$fname"
    # W-4: force a re-fetch — but never delete real files during a --dry-run preview
    if [ "$FORCE" = "1" ] && [ "${EPISTEME_DRY_RUN:-0}" != "1" ]; then rm -f "$dest/$rel"; fi
    if size_match_skip "$dest/$rel" "$url"; then
        log INFO "skip (size-matched): $rel"
    else
        lines+=("$url"$'\t'"$rel")
    fi
done

if [ "${#lines[@]}" -eq 0 ]; then
    log INFO "ontologies: up to date"
    write_sync_stamp "$dest"
    exit 0
fi

if printf '%s\n' "${lines[@]}" | cap_urls "$MAX_FILES" | http_fetch "$dest"; then
    write_sync_stamp "$dest"
else
    die "ontologies: fetch failed"
fi
