#!/usr/bin/env bash
# Episteme SP3 acquisition wrapper: Europe PMC preprint-abstracts feed bulk
# download. Thin shell over scripts/data/_lib/common.sh — directory listing +
# size-skip + fetch only (no extract/parse). Flat EBI directory. The current
# (still-accumulating) month is deferred unless --include-current is passed.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../_lib/common.sh
. "$HERE/../../_lib/common.sh"

MAX_FILES=""
FORCE=0
INCLUDE_CURRENT=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)         export EPISTEME_DRY_RUN=1; shift ;;
        --max-files)       MAX_FILES="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --force)           FORCE=1; shift ;;
        --include-current) INCLUDE_CURRENT=1; shift ;;
        --reason)          shift; [ $# -gt 0 ] && shift ;;   # run_pipeline already enforced it
        *)                 log WARN "download_europepmc_abstracts.sh: ignoring $1"; shift ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR EUROPEPMC_ABSTRACTS_BASE

dest="$(resolve_dest europepmc abstracts)"

mapfile -t all_files < <(list_manifest "$EUROPEPMC_ABSTRACTS_BASE" '\.zip$')

ym="$(date -u +%Y%m)"
files=()
for f in "${all_files[@]}"; do
    [ -n "$f" ] || continue
    if [ "$INCLUDE_CURRENT" != "1" ]; then
        case "$f" in
            *_"$ym".*)
                log INFO "abstracts: deferring current (unpublished) month: $f"
                continue
                ;;
        esac
    fi
    files+=("$f")
done

if [ "${#files[@]}" -eq 0 ]; then
    if [ "${#all_files[@]}" -gt 0 ]; then
        log INFO "abstracts: only the current (unpublished) month is listed; pass --include-current to fetch it"
        write_sync_stamp "$dest"
        exit 0
    fi
    die "europepmc abstracts: no files at $EUROPEPMC_ABSTRACTS_BASE"
fi

lines=()
for f in "${files[@]}"; do
    [ -n "$f" ] || continue
    u="$EUROPEPMC_ABSTRACTS_BASE/$f"
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
    log INFO "europepmc abstracts: up to date"
    write_sync_stamp "$dest"
    exit 0
fi

if printf '%s\n' "${lines[@]}" | cap_urls "$MAX_FILES" | http_fetch "$dest"; then
    write_sync_stamp "$dest"
else
    die "europepmc abstracts: fetch failed"
fi
