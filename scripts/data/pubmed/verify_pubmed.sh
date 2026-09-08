#!/usr/bin/env bash
# Episteme SP3: local MD5 verifier for the PubMed acquisition set. Thin shell
# over scripts/data/_lib/common.sh — no colour codes, log-based, no network in
# plain mode. Checks each local pubmed*.xml.gz against its .md5 (published by
# NCBI, kept under <dest>/md5/ by download_pubmed.sh; falls back to a sibling
# .md5). Exit 1 iff any file FAILs; a missing .md5 alone is a WARN, exit 0.
#
# Usage: verify_pubmed.sh [baseline|updates|all] [--repair] [--dry-run]
#   --repair  re-fetch every FAIL / missing-.md5 file (needs PUBMED_FTP_BASE);
#             honours --dry-run (lists, deletes/fetches nothing).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"

MODE="all"
REPAIR=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)            export EPISTEME_DRY_RUN=1; shift ;;
        --repair)             REPAIR=1; shift ;;
        --reason)             shift; [ $# -gt 0 ] && shift ;;
        baseline|updates|all) MODE="$1"; shift ;;
        *)                    log WARN "verify_pubmed.sh: ignoring $1"; shift ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR
[ "$REPAIR" = "1" ] && require_env PUBMED_FTP_BASE

dest="$(resolve_dest pubmed)"
md5_dir="$dest/md5"

case "$MODE" in
    baseline) modes=(baseline) ;;
    updates)  modes=(updates) ;;
    all)      modes=(baseline updates) ;;
    *)        die "pubmed verify: bad mode '$MODE' (baseline|updates|all)" ;;
esac

# mode -> upstream subdirectory (for --repair URL construction).
upstream_sub() {
    case "$1" in
        baseline) printf 'baseline\n' ;;
        updates)  printf 'updatefiles\n' ;;
    esac
}

md5_hex() { # md5_hex FILE -> first 32-hex token, or empty
    grep -oE '[a-fA-F0-9]{32}' "$1" 2>/dev/null | head -n1
}

total_pass=0
total_fail=0
total_missing=0
repair_entries=()

for m in "${modes[@]}"; do
    d="$dest/$m"
    if [ ! -d "$d" ]; then
        log WARN "pubmed verify: no directory $d — skipping $m"
        continue
    fi

    m_pass=0
    m_fail=0
    m_missing=0

    while IFS= read -r file; do
        [ -n "$file" ] || continue
        base="$(basename "$file")"
        md5_file="$md5_dir/$base.md5"
        [ -f "$md5_file" ] || md5_file="$file.md5"

        if [ ! -f "$md5_file" ]; then
            log WARN "missing .md5: $m/$base"
            m_missing=$((m_missing + 1))
            [ "$REPAIR" = "1" ] && repair_entries+=("$m"$'\t'"$base")
            continue
        fi

        expected="$(md5_hex "$md5_file")"
        actual="$(md5sum "$file" | awk '{print $1}')"

        if [ -z "$expected" ]; then
            log WARN "unparseable .md5: $m/$base"
            m_missing=$((m_missing + 1))
            [ "$REPAIR" = "1" ] && repair_entries+=("$m"$'\t'"$base")
        elif [ "$expected" = "$actual" ]; then
            m_pass=$((m_pass + 1))
        else
            log ERROR "FAIL: $m/$base (expected $expected, got $actual)"
            m_fail=$((m_fail + 1))
            [ "$REPAIR" = "1" ] && repair_entries+=("$m"$'\t'"$base")
        fi
    done < <(find "$d" -maxdepth 1 -name 'pubmed[0-9]*n[0-9]*.xml.gz' | sort)

    log INFO "pubmed verify [$m]: pass=$m_pass fail=$m_fail missing-md5=$m_missing"
    total_pass=$((total_pass + m_pass))
    total_fail=$((total_fail + m_fail))
    total_missing=$((total_missing + m_missing))
done

log INFO "pubmed verify [all]: pass=$total_pass fail=$total_fail missing-md5=$total_missing"

if [ "$REPAIR" = "1" ] && [ "${#repair_entries[@]}" -gt 0 ]; then
    log INFO "pubmed verify: --repair — ${#repair_entries[@]} file(s) to re-fetch"
    fetch_lines=()
    for entry in "${repair_entries[@]}"; do
        m="${entry%%$'\t'*}"
        f="${entry#*$'\t'}"
        sub="$(upstream_sub "$m")"
        if [ "${EPISTEME_DRY_RUN:-0}" != "1" ]; then
            rm -f "$dest/$m/$f" "$md5_dir/$f.md5" "$dest/$m/$f.md5"
        fi
        fetch_lines+=("$PUBMED_FTP_BASE/$sub/$f"$'\t'"$m/$f")
        fetch_lines+=("$PUBMED_FTP_BASE/$sub/$f.md5"$'\t'"md5/$f.md5")
    done
    if printf '%s\n' "${fetch_lines[@]}" | http_fetch "$dest"; then
        log INFO "pubmed verify: --repair fetch complete — re-run verify_pubmed.sh to confirm"
    else
        die "pubmed verify: --repair fetch failed"
    fi
fi

if [ "$total_fail" -gt 0 ]; then
    exit 1
fi
exit 0
