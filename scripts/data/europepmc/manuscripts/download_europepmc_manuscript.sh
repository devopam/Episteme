#!/usr/bin/env bash
# Episteme SP3 acquisition wrapper: Europe PMC author-manuscripts feed bulk
# download. Thin shell over scripts/data/_lib/common.sh — directory listing +
# size-skip + fetch only (no extract/parse). Flat EBI directory.
# FORMAT positional: xml (default) | txt.
# MODE   positional: all (default) | baseline | incr.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../_lib/common.sh
. "$HERE/../../_lib/common.sh"

FMT="xml"
MODE="all"
MAX_FILES=""
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)         export EPISTEME_DRY_RUN=1; shift ;;
        --max-files)       MAX_FILES="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --force)           FORCE=1; shift ;;
        --reason)          shift; [ $# -gt 0 ] && shift ;;   # run_pipeline already enforced it
        xml|txt)           FMT="$1"; shift ;;
        all|baseline|incr) MODE="$1"; shift ;;
        *)                 log WARN "download_europepmc_manuscript.sh: ignoring $1"; shift ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR EUROPEPMC_MANUSCRIPT_BASE

dest="$(resolve_dest europepmc manuscripts)"

mapfile -t raw < <(list_manifest "$EUROPEPMC_MANUSCRIPT_BASE" "^author_manuscript_${FMT}\\..*\\.(tar\\.gz|filelist\\.csv|filelist\\.txt)\$")
files=()
for f in "${raw[@]}"; do
    [ -n "$f" ] || continue
    if [ "$MODE" != "all" ]; then
        case "$MODE" in
            baseline) case "$f" in *.baseline.*) ;; *) continue ;; esac ;;
            incr)     case "$f" in *.incr.*) ;;     *) continue ;; esac ;;
        esac
    fi
    files+=("$f")
done
[ "${#files[@]}" -gt 0 ] || die "europepmc manuscripts: no files at $EUROPEPMC_MANUSCRIPT_BASE (fmt=$FMT mode=$MODE)"

# C1: --max-files caps the RESOLVED set here, before the --force prune loop —
# otherwise --force deletes the whole set and only N are re-fetched.
if [ -n "$MAX_FILES" ]; then
    mapfile -t files < <(printf '%s\n' "${files[@]}" | cap_urls "$MAX_FILES")
fi

lines=()
for f in "${files[@]}"; do
    [ -n "$f" ] || continue
    u="$EUROPEPMC_MANUSCRIPT_BASE/$f"
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
    log INFO "europepmc manuscripts: up to date"
    write_sync_stamp "$dest"
    exit 0
fi

if printf '%s\n' "${lines[@]}" | http_fetch "$dest"; then
    write_sync_stamp "$dest"
else
    die "europepmc manuscripts: fetch failed"
fi
