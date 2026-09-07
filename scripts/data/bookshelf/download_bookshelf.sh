#!/usr/bin/env bash
# Episteme SP3 acquisition wrapper: NCBI LitArch / Bookshelf OA packages. Thin
# shell over scripts/data/_lib/common.sh — manifest fetch + size-skip + fetch
# only (no extract/parse). The base URL comes from $BOOKSHELF_BASE
# (scripts/data/_lib/sources.env). LitArch is a hashed 00..ff tree with no
# usable directory listing, so the package list is read from the base's
# file_list.txt manifest (file_list.csv is the fallback). No MODE.
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
        *)           log WARN "download_bookshelf.sh: ignoring $1"; shift ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR BOOKSHELF_BASE

dest="$(resolve_dest bookshelf)"

manifest="$(curl -sSL --fail --connect-timeout 30 --max-time 180 "$BOOKSHELF_BASE/file_list.txt")" \
    || die "bookshelf: cannot fetch $BOOKSHELF_BASE/file_list.txt"

# Column 1 (TAB-separated): keep *.tar.gz, strip CR and leading './', sort -u.
paths="$(printf '%s\n' "$manifest" \
    | awk -F'\t' '{gsub(/\r/,"",$1); if ($1 ~ /\.tar\.gz$/) print $1}' \
    | sed 's#^\./##' | sort -u)"

if [ -z "$paths" ]; then
    log INFO "bookshelf: no .tar.gz in file_list.txt — trying file_list.csv"
    manifest="$(curl -sSL --fail --connect-timeout 30 --max-time 180 "$BOOKSHELF_BASE/file_list.csv")" \
        || die "bookshelf: cannot fetch $BOOKSHELF_BASE/file_list.csv"
    paths="$(printf '%s\n' "$manifest" \
        | awk -F',' 'NR>1 {gsub(/"/,"",$1); gsub(/\r/,"",$1); if ($1 ~ /\.tar\.gz$/) print $1}' \
        | sed 's#^\./##' | sort -u)"
fi

[ -n "$paths" ] || die "bookshelf: no .tar.gz paths in file_list"

lines=()
while IFS= read -r rel || [ -n "$rel" ]; do
    [ -n "$rel" ] || continue
    url="$BOOKSHELF_BASE/$rel"
    relpath="packages/$rel"
    # force a re-fetch — but never delete real files during a --dry-run preview
    if [ "$FORCE" = "1" ] && [ "${EPISTEME_DRY_RUN:-0}" != "1" ]; then rm -f "$dest/$relpath"; fi
    if size_match_skip "$dest/$relpath" "$url"; then
        log INFO "skip (size-matched): $relpath"
    else
        lines+=("$url"$'\t'"$relpath")
    fi
done <<< "$paths"

if [ "${#lines[@]}" -eq 0 ]; then
    log INFO "bookshelf: up to date"
    write_sync_stamp "$dest"
    exit 0
fi

if printf '%s\n' "${lines[@]}" | cap_urls "$MAX_FILES" | http_fetch "$dest"; then
    write_sync_stamp "$dest"
else
    die "bookshelf: fetch failed"
fi
