#!/usr/bin/env bash
# Episteme SP3 acquisition wrapper: AACT (ClinicalTrials.gov) daily snapshot
# download. Thin shell over scripts/data/_lib/common.sh — HTML page scrape +
# size-skip + fetch only (no extract/parse). Download-only, forever ("volatile"
# / Phase-1-deferred; same carve-out class as mesh). The page URL comes from
# $AACT_DOWNLOADS (scripts/data/_lib/sources.env); the download links are read
# out of that page at run time (not literals in this file). No MODE.
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
        *)           log WARN "download_aact.sh: ignoring $1"; shift ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR AACT_DOWNLOADS

dest="$(resolve_dest aact)"
host="${AACT_DOWNLOADS%/downloads}"

page="$(curl -sSL --fail --connect-timeout 30 --max-time 120 "$AACT_DOWNLOADS")" \
    || die "aact: cannot fetch $AACT_DOWNLOADS"

# The downloads page is a Turbo app with no href="…zip"; the real links are
# date-keyed, extension-less endpoints under /static/{static_db_copies,
# exported_files}/daily/<DATE> that each serve a zip.
mapfile -t paths < <(printf '%s\n' "$page" \
    | grep -oE 'href="/static/(static_db_copies|exported_files)/daily/[0-9][0-9-]*[^"]*"' \
    | sed -E 's/^href="//; s/"$//' | sort -u)

# Ruling ZF-2: aact is volatile / Phase-1-deferred and nothing in Phase 0
# blocks on it. A scrape that resolves nothing is a page-layout change to
# chase in SP3-followup, not a hard failure — warn and exit 0, do not stamp.
if [ "${#paths[@]}" -eq 0 ]; then
    log WARN "aact: resolved 0 download links (page layout changed — SP3-followup); volatile deferred source"
    exit 0
fi

planned=()
for path in "${paths[@]}"; do
    [ -n "$path" ] || continue
    seg="${path##*/daily/}"
    date_seg="${seg%%\?*}"
    case "$path" in
        *static_db_copies*) name="postgres_${date_seg}.zip" ;;
        *exported_files*)   name="flatfiles_${date_seg}.zip" ;;
        *)                  log WARN "aact: unrecognized link $path (skipping)"; continue ;;
    esac
    # I2: date_seg comes from a scraped href capture — reject a traversal before
    # it reaches the on-disk relpath.
    _safe_rel "$name" || die "aact: unsafe filename '$name' from scraped link"
    planned+=("$host$path"$'\t'"$name")
done

if [ "${#planned[@]}" -eq 0 ]; then
    log WARN "aact: resolved 0 download links (page layout changed — SP3-followup); volatile deferred source"
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
    log INFO "aact: up to date"
    write_sync_stamp "$dest"
    exit 0
fi

if printf '%s\n' "${lines[@]}" | http_fetch "$dest"; then
    write_sync_stamp "$dest"
else
    die "aact: fetch failed"
fi
