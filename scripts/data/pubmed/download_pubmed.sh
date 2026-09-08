#!/usr/bin/env bash
# Episteme SP3 acquisition wrapper: PubMed annual-baseline / daily-update bulk
# download. Thin shell over scripts/data/_lib/common.sh — directory listing +
# size-skip + fetch only (no extract/parse). Each xml.gz is fetched alongside
# its NCBI-published .md5 sibling (kept under <dest>/md5/ for verify_pubmed.sh).
# MODE positional: baseline (default) | updates.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"

MODE="baseline"
MAX_FILES=""
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)        export EPISTEME_DRY_RUN=1; shift ;;
        --max-files)      MAX_FILES="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --force)          FORCE=1; shift ;;
        --reason)         shift; [ $# -gt 0 ] && shift ;;   # run_pipeline already enforced it
        baseline|updates) MODE="$1"; shift ;;
        *)                log WARN "download_pubmed.sh: ignoring $1"; shift ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR PUBMED_FTP_BASE

dest="$(resolve_dest pubmed)"

# MODE -> upstream subdirectory (baseline files vs daily update files).
case "$MODE" in
    baseline) sub="baseline" ;;
    updates)  sub="updatefiles" ;;
    *)        die "pubmed: bad mode '$MODE' (baseline|updates)" ;;
esac

mapfile -t xmls < <(list_manifest "$PUBMED_FTP_BASE/$sub" 'pubmed[0-9]+n[0-9]+\.xml\.gz$')
[ "${#xmls[@]}" -gt 0 ] || die "pubmed: no xml.gz files at $PUBMED_FTP_BASE/$sub (mode=$MODE)"

# Each data file gets two planned entries: the xml.gz under <mode>/ and its
# NCBI-published .md5 sibling under md5/ (same upstream directory).
planned=()
for f in "${xmls[@]}"; do
    [ -n "$f" ] || continue
    planned+=("$PUBMED_FTP_BASE/$sub/$f"$'\t'"$MODE/$f")
    planned+=("$PUBMED_FTP_BASE/$sub/$f.md5"$'\t'"md5/$f.md5")
done

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
    log INFO "pubmed: up to date"
    write_sync_stamp "$dest"
    exit 0
fi

if printf '%s\n' "${lines[@]}" | cap_urls "$MAX_FILES" | http_fetch "$dest"; then
    write_sync_stamp "$dest"
else
    die "pubmed: fetch failed"
fi
