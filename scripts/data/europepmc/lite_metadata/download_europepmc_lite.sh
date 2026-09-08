#!/usr/bin/env bash
# Episteme SP3 acquisition wrapper: Europe PMC Lite-Metadata feed bulk download.
# Thin shell over scripts/data/_lib/common.sh — directory listing + size-skip +
# fetch only (no extract/parse). Flat EBI directory; no MODE.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../_lib/common.sh
. "$HERE/../../_lib/common.sh"

MAX_FILES=""
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)   export EPISTEME_DRY_RUN=1; shift ;;
        --max-files) MAX_FILES="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --force)     FORCE=1; shift ;;
        --reason)    shift; [ $# -gt 0 ] && shift ;;   # run_pipeline already enforced it
        *)           log WARN "download_europepmc_lite.sh: ignoring $1"; shift ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR EUROPEPMC_LITE_BASE

dest="$(resolve_dest europepmc lite_metadata)"

mapfile -t files < <(list_manifest "$EUROPEPMC_LITE_BASE" '\.(tgz|tar\.gz|zip)$')
[ "${#files[@]}" -gt 0 ] || die "europepmc lite_metadata: no files at $EUROPEPMC_LITE_BASE"

# C1: --max-files caps the RESOLVED set here, before the --force prune loop —
# otherwise --force deletes the whole set and only N are re-fetched.
if [ -n "$MAX_FILES" ]; then
    mapfile -t files < <(printf '%s\n' "${files[@]}" | cap_urls "$MAX_FILES")
fi

lines=()
for f in "${files[@]}"; do
    [ -n "$f" ] || continue
    u="$EUROPEPMC_LITE_BASE/$f"
    # W-4: force a re-fetch — but never delete real files during a --dry-run preview
    if [ "$FORCE" = "1" ] && [ "${EPISTEME_DRY_RUN:-0}" != "1" ]; then
        rm -f "$dest/$f"
    fi
    if size_match_skip "$dest/$f" "$u"; then
        log INFO "skip (size-matched): $f"
    else
        lines+=("$u")
    fi
done

if [ "${#lines[@]}" -eq 0 ]; then
    log INFO "europepmc lite_metadata: up to date"
    write_sync_stamp "$dest"
    exit 0
fi

if printf '%s\n' "${lines[@]}" | http_fetch "$dest"; then
    write_sync_stamp "$dest"
else
    die "europepmc lite_metadata: fetch failed"
fi
