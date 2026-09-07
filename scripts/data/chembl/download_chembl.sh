#!/usr/bin/env bash
# Episteme SP3 acquisition wrapper: ChEMBL full-release bulk download.
# Thin shell over scripts/data/_lib/common.sh — discovery + size-skip + fetch
# only (no extract/parse). MODE positional: default (SQLite + SDF + chemreps +
# ancillaries) or all (adds postgresql / mysql / h5 / fps dumps).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/data/_lib/common.sh
. "$HERE/../_lib/common.sh"

MODE="default"
MAX_FILES=""
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)   export EPISTEME_DRY_RUN=1; shift ;;
        --max-files) MAX_FILES="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --force)     FORCE=1; shift ;;
        --reason)    shift; [ $# -gt 0 ] && shift ;;   # run_pipeline already enforced it
        default|all) MODE="$1"; shift ;;
        *)           log WARN "download_chembl.sh: ignoring $1"; shift ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR

dest="$(resolve_dest chembl)"

# One discover_manifest call for the union of every wanted pattern (W-2).
patterns=(
    '^LICENSE$'
    '^README$'
    '^REQUIRED'
    '^checksums\.txt$'
    'chembl_[0-9]+_release_notes\.txt$'
    'chembl_[0-9]+_sqlite\.tar\.gz$'
    'chembl_[0-9]+\.sdf\.gz$'
    'chembl_[0-9]+_chemreps\.txt\.gz$'
    'chembl_uniprot_mapping\.txt$'
    'schema_documentation\.(html|txt)$'
)
if [ "$MODE" = "all" ]; then
    patterns+=(
        'chembl_[0-9]+_postgresql\.tar\.gz$'
        'chembl_[0-9]+_mysql\.tar\.gz$'
        'chembl_[0-9]+\.h5$'
        'chembl_[0-9]+\.fps\.gz$'
    )
fi

mapfile -t files < <(discover_manifest "$CHEMBL_BASE" "${patterns[@]}" | sort -u)
[ "${#files[@]}" -gt 0 ] || die "chembl: no files matched at $CHEMBL_BASE (listing format changed?)"

urls=()
for f in "${files[@]}"; do
    [ -n "$f" ] || continue
    u="$CHEMBL_BASE/$f"
    [ "$FORCE" = "1" ] && rm -f "$dest/$f"          # W-4: force a re-fetch
    if size_match_skip "$dest/$f" "$u"; then
        log INFO "skip (size-matched): $f"
    else
        urls+=("$u")
    fi
done

if [ "${#urls[@]}" -eq 0 ]; then
    log INFO "chembl: up to date"
    write_sync_stamp "$dest"
    exit 0
fi

if printf '%s\n' "${urls[@]}" | cap_urls "$MAX_FILES" | http_fetch "$dest"; then
    write_sync_stamp "$dest"
else
    die "chembl: fetch failed"
fi
