#!/usr/bin/env bash
# Episteme SP3 acquisition wrapper: ClinVar bulk download. Thin shell over
# scripts/data/_lib/common.sh — directory listing + size-skip + fetch only.
# MODE positional: tsv (default) | vcf38 | vcf37 | xml | all.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"

MODE="tsv"
MAX_FILES=""
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)              export EPISTEME_DRY_RUN=1; shift ;;
        --max-files)            MAX_FILES="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --force)                FORCE=1; shift ;;
        --reason)               shift; [ $# -gt 0 ] && shift ;;   # run_pipeline already enforced it
        tsv|vcf38|vcf37|xml|all) MODE="$1"; shift ;;
        *)                      log WARN "download_clinvar.sh: ignoring $1"; shift ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR CLINVAR_BASE

dest="$(resolve_dest clinvar)"

# MODE -> ClinVar subdirectory set (from the batch script).
case "$MODE" in
    tsv)   subs=(tab_delimited) ;;
    vcf38) subs=(vcf_GRCh38) ;;
    vcf37) subs=(vcf_GRCh37) ;;
    xml)   subs=(xml) ;;
    all)   subs=(tab_delimited vcf_GRCh38 vcf_GRCh37) ;;
    *)     die "clinvar: bad mode '$MODE' (tsv|vcf38|vcf37|xml|all)" ;;
esac

planned=()
for sub in "${subs[@]}"; do
    mapfile -t fs < <(list_manifest "$CLINVAR_BASE/$sub" '\.(gz|tsv|txt|vcf|xml)(\.gz)?$')
    for f in "${fs[@]}"; do
        [ -n "$f" ] || continue
        planned+=("$CLINVAR_BASE/$sub/$f"$'\t'"$sub/$f")
    done
done
[ "${#planned[@]}" -gt 0 ] || die "clinvar: no files resolved under $CLINVAR_BASE (mode=$MODE)"

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
    log INFO "clinvar: up to date"
    write_sync_stamp "$dest"
    exit 0
fi

if printf '%s\n' "${lines[@]}" | cap_urls "$MAX_FILES" | http_fetch "$dest"; then
    write_sync_stamp "$dest"
else
    die "clinvar: fetch failed"
fi
