#!/usr/bin/env bash
# Episteme SP7 acquisition wrapper: CDISC Controlled Terminology (CT), as published
# by NCI Enterprise Vocabulary Services under $CDISC_CT_BASE/<Package>/.
# Thin shell over scripts/data/_lib/common.sh — HEAD probe + size-skip + fetch.
#
# Layout written (read by src/episteme/data/cdisc_ct/serialize_cdisc_ct.py):
#   01_raw/cdisc_ct/<Package>/<YYYY-MM-DD>/<Package>_Terminology.txt
#   + PROVENANCE.txt and last_sync_utc.txt in each release folder.
# Packages, in fixed order: SDTM SEND ADaM Define-XML Protocol. --max-files N caps
# the PACKAGES (first N in that order). Release dates differ per package, so each
# package gets its own dated folder, derived from the HEAD response's Last-Modified
# (the file content carries no release date). Older release folders are never deleted.
#
# NCI's download site is a JavaScript app: a missing path answers HTTP 200 with a
# small text/html fallback page (verified 2026-09-28), so availability is decided by
# Content-Type (must be text/plain), never by status code. After download the first
# line must be the exact 8-column tab-separated header; otherwise the file is removed.
#
# Licence: NCI states CDISC Terminology is free to use without licensing restrictions;
# recorded in PROVENANCE.txt (governance decision 2026-09-28: public_domain override).
# A missing or malformed package is a WARN and is skipped (rc 0); a failed transfer
# makes the run exit 1 after the remaining packages have been tried.
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
        -*)          log WARN "download_cdisc_ct.sh: ignoring $1"; shift ;;
        *)           die "cdisc_ct: unknown mode '$1' (this source has no modes)" ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR CDISC_CT_BASE

dest="$(resolve_dest cdisc_ct)"

EXPECTED_HEADER="Code"$'\t'"Codelist Code"$'\t'"Codelist Extensible (Yes/No)"$'\t'"Codelist Name"$'\t'"CDISC Submission Value"$'\t'"CDISC Synonym(s)"$'\t'"CDISC Definition"$'\t'"NCI Preferred Term"
LICENCE_STATEMENT="NCI EVS: CDISC Terminology is free to use without licensing restrictions."

mapfile -t packages < <(printf '%s\n' SDTM SEND ADaM Define-XML Protocol | cap_urls "$MAX_FILES")

_header_value() { # _header_value NAME <<< "$hdr" -> last value of a (case-insensitive) header
    awk -v want="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" '
        { i = index($0, ":"); if (!i) next
          k = tolower(substr($0, 1, i - 1))
          if (k == want) { v = substr($0, i + 1); sub(/^[ \t]+/, "", v); sub(/[ \t]+$/, "", v); last = v } }
        END { if (last != "") print last }'
}

failed=0
for pkg in "${packages[@]}"; do
    url="$CDISC_CT_BASE/$pkg/$pkg%20Terminology.txt"
    # Headers only (-I) — never a body. No -L: runs under --dry-run too.
    # pipefail: a curl failure (DNS, timeout, refused) is the pipeline's status.
    if ! hdr="$(curl -sSI --connect-timeout 20 --max-time 60 "$url" 2>/dev/null | tr -d '\r')"; then
        log WARN "cdisc_ct: could not reach NCI for $pkg ($url); skipping"
        continue
    fi
    ctype="$(printf '%s\n' "$hdr" | _header_value content-type)"
    case "$ctype" in
        text/plain*) ;;
        *) log WARN "cdisc_ct: $pkg not available (Content-Type '${ctype:-none}' at $url); skipping"
           continue ;;
    esac

    # The -n guard is load-bearing: `date -u -d ""` prints TODAY's date.
    lastmod="$(printf '%s\n' "$hdr" | _header_value last-modified)"
    rdate=""
    [ -n "$lastmod" ] && rdate="$(date -u -d "$lastmod" +%Y-%m-%d 2>/dev/null || true)"
    case "$rdate" in
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
        *) log WARN "cdisc_ct: $pkg has no parseable Last-Modified ('${lastmod:-none}'); skipping"
           continue ;;
    esac

    reldir="$dest/$pkg/$rdate"
    fname="${pkg}_Terminology.txt"
    out="$reldir/$fname"
    part="$out.part"

    # --force skips the size check; the existing file is only replaced after a
    # validated download (below), never deleted up front.
    if [ "$FORCE" != "1" ] && size_match_skip "$out" "$url"; then
        log INFO "cdisc_ct: $pkg release $rdate up to date"
        continue
    fi

    if [ "${EPISTEME_DRY_RUN:-0}" = "1" ]; then
        log INFO "DRY: would fetch $url -> $out (Last-Modified: $lastmod)"
        continue
    fi

    # Atomic replace: fetch to <file>.part in the same folder (http_fetch writes
    # to DEST_DIR/<relpath>), validate, then mv over <file>. A stale .part is
    # removed first so curl's resume (-C -) cannot append to it. On any failure
    # only the .part goes; an existing good file and its PROVENANCE.txt stay.
    rm -f "$part"
    # Pipeline => subshell: a `die` inside http_fetch fails this package only.
    if ! printf '%s\t%s\n' "$url" "$fname.part" | http_fetch "$reldir" || [ ! -s "$part" ]; then
        log WARN "cdisc_ct: $pkg fetch failed ($url)"
        rm -f "$part"
        rmdir "$reldir" "$dest/$pkg" 2>/dev/null || true   # only if left empty
        failed=1
        continue
    fi

    first="$(head -n1 "$part" 2>/dev/null | tr -d '\r')"
    first="${first#$'\xef\xbb\xbf'}"   # tolerate a UTF-8 BOM
    if [ "$first" != "$EXPECTED_HEADER" ]; then
        log WARN "cdisc_ct: $pkg unexpected header in downloaded $fname; discarding it"
        rm -f "$part"
        rmdir "$reldir" "$dest/$pkg" 2>/dev/null || true   # only if left empty
        continue
    fi

    if ! mv -f "$part" "$out"; then
        log WARN "cdisc_ct: $pkg could not move $part into place"
        rm -f "$part"
        failed=1
        continue
    fi

    {
        printf 'source: CDISC Controlled Terminology (NCI EVS), package %s\n' "$pkg"
        printf 'source_url: %s\n' "$url"
        printf 'last_modified: %s\n' "$lastmod"
        printf 'release_date: %s\n' "$rdate"
        printf 'retrieved_at: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf 'licence: %s\n' "$LICENCE_STATEMENT"
    } > "$reldir/PROVENANCE.txt"
    write_sync_stamp "$reldir"
    log INFO "cdisc_ct: $pkg release $rdate fetched"
done

[ "$failed" = "0" ] || die "cdisc_ct: one or more packages failed to transfer"
exit 0
