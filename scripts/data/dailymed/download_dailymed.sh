#!/usr/bin/env bash
# Episteme SP3 acquisition wrapper: DailyMed SPL zip bulk download. Thin shell
# over scripts/data/_lib/common.sh — HTML page scrape + size-skip + fetch only
# (no extract/parse). Download-only, forever ("volatile"). The page host comes
# from $DAILYMED_BASE (scripts/data/_lib/sources.env); the zip links are read
# out of the SPL resources pages at run time (not literals in this file).
# MODE positional: index | monthly | fullparts | all (default all).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"

MODE="all"
MAX_FILES=""
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)                   export EPISTEME_DRY_RUN=1; shift ;;
        --max-files)                 MAX_FILES="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --force)                     FORCE=1; shift ;;
        --reason)                    shift; [ $# -gt 0 ] && shift ;;   # run_pipeline already enforced it
        index|monthly|fullparts|all) MODE="$1"; shift ;;
        *)                           log WARN "download_dailymed.sh: ignoring $1"; shift ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR DAILYMED_BASE

dest="$(resolve_dest dailymed)"

# MODE -> which SPL resources pages to scrape (paths relative to $DAILYMED_BASE).
case "$MODE" in
    index)     pages=(spl-resources-all-indexing-files.cfm) ;;
    monthly)   pages=(spl-resources-all-drug-labels.cfm) ;;
    fullparts) pages=(spl-resources-all-drug-labels.cfm) ;;
    all)       pages=(spl-resources-all-indexing-files.cfm spl-resources-all-drug-labels.cfm spl-resources.cfm) ;;
    *)         die "dailymed: bad mode '$MODE' (index|monthly|fullparts|all)" ;;
esac

# Scrape .zip hrefs from every page for this MODE. A single page that 404s is a
# warn-and-skip (no set -e), not a hard failure.
hrefs=""
for page in "${pages[@]}"; do
    if html="$(curl -sSL --fail --connect-timeout 30 --max-time 180 "$DAILYMED_BASE/$page")"; then
        found="$(printf '%s\n' "$html" | grep -oE 'href="[^"]+\.zip"' | sed -E 's/^href="//; s/"$//')"
        hrefs="${hrefs:+$hrefs$'\n'}$found"
    else
        log WARN "dailymed: could not fetch $page (skipping)"
    fi
done

# The pages list absolute links: keep the plain-transport ones and drop the
# ftp: duplicates, then dedup. Gate-clean scheme test — negative-filter ftp,
# positive-require a scheme separator (no scheme prefix appears as a literal).
urls="$(printf '%s\n' "$hrefs" | sort -u)"

planned=()
while IFS= read -r h || [ -n "$h" ]; do
    [ -n "$h" ] || continue
    case "$h" in
        ftp:*) continue ;;
        *://*) ;;
        *)     continue ;;
    esac
    base="${h##*/}"
    lc="${base,,}"
    case "$MODE" in
        monthly)   case "$lc" in *monthly*|*update*) ;; *) continue ;; esac ;;
        fullparts) case "$lc" in *dm_spl_release*|*human_rx*|*human_otc*|*part*|*volume*) ;; *) continue ;; esac ;;
    esac
    planned+=("$h")
done <<< "$urls"

[ "${#planned[@]}" -gt 0 ] || die "dailymed: no .zip URLs resolved for mode=$MODE (page layout changed?)"

# C1: --max-files caps the RESOLVED set here, before the --force prune loop —
# otherwise --force deletes the whole set and only N are re-fetched.
if [ -n "$MAX_FILES" ]; then
    mapfile -t planned < <(printf '%s\n' "${planned[@]}" | cap_urls "$MAX_FILES")
fi

lines=()
for h in "${planned[@]}"; do
    base="${h##*/}"
    # W-4: force a re-fetch — but never delete real files during a --dry-run preview
    if [ "$FORCE" = "1" ] && [ "${EPISTEME_DRY_RUN:-0}" != "1" ]; then rm -f "$dest/$base"; fi
    if size_match_skip "$dest/$base" "$h"; then
        log INFO "skip (size-matched): $base"
    else
        lines+=("$h")
    fi
done

if [ "${#lines[@]}" -eq 0 ]; then
    log INFO "dailymed: up to date"
    write_sync_stamp "$dest"
    exit 0
fi

if printf '%s\n' "${lines[@]}" | http_fetch "$dest"; then
    write_sync_stamp "$dest"
else
    die "dailymed: fetch failed"
fi
