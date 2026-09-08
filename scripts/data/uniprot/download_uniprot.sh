#!/usr/bin/env bash
# Episteme SP3 acquisition wrapper: UniProtKB Swiss-Prot bulk download.
# Thin shell over scripts/data/_lib/common.sh — mirror probe + size-skip + fetch
# only (no extract/parse). Swiss-Prot product set only; TrEMBL is not fetched.
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
        *)           log WARN "download_uniprot.sh: ignoring $1"; shift ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR UNIPROT_MIRRORS

dest="$(resolve_dest uniprot swissprot)"

# W-3: pick the first mirror that serves reldate.txt with a 200. Runs even under
# --dry-run (cheap) so the chosen mirror is always printed.
BASE=""
code=""
IFS='|' read -r -a mirrors <<< "${UNIPROT_MIRRORS:-}"
# M3: UNIPROT_BASE (sources.env + Settings) is otherwise dead in the shell layer.
# Seed the probe list with it so an operator's .env override actually takes
# effect. Prepend only if not already present (it is byte-identical to the first
# mirror in the shipped sources.env — avoid probing the same URL twice).
if [ -n "${UNIPROT_BASE:-}" ]; then
    _seen=0
    for m in "${mirrors[@]}"; do [ "$m" = "$UNIPROT_BASE" ] && _seen=1; done
    [ "$_seen" = "1" ] || mirrors=("$UNIPROT_BASE" "${mirrors[@]}")
fi
for m in "${mirrors[@]}"; do
    code="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 20 --max-time 45 "$m/reldate.txt" 2>/dev/null || true)"
    [ "$code" = "200" ] && { BASE="$m"; break; }
done
[ -n "$BASE" ] || die "uniprot: no mirror reachable"
log INFO "uniprot mirror: $BASE"

# U-1: ancillaries first so --max-files 2 grabs the two cheap files.
files=(reldate.txt LICENSE README uniprot.xsd uniprot_sprot.xml.gz uniprot_sprot.fasta.gz uniprot_sprot.dat.gz uniprot_sprot_varsplic.fasta.gz)

# C1: --max-files caps the RESOLVED set here, before the --force prune loop —
# otherwise --force deletes the whole set and only N are re-fetched.
if [ -n "$MAX_FILES" ]; then
    mapfile -t files < <(printf '%s\n' "${files[@]}" | cap_urls "$MAX_FILES")
fi

urls=()
for f in "${files[@]}"; do
    u="$BASE/$f"
    # W-4: force a re-fetch — but never delete real files during a --dry-run preview
    if [ "$FORCE" = "1" ] && [ "${EPISTEME_DRY_RUN:-0}" != "1" ]; then rm -f "$dest/$f"; fi
    if size_match_skip "$dest/$f" "$u"; then
        log INFO "skip (size-matched): $f"
    else
        urls+=("$u")
    fi
done

if [ "${#urls[@]}" -eq 0 ]; then
    log INFO "uniprot: up to date"
    write_sync_stamp "$dest"
    exit 0
fi

if printf '%s\n' "${urls[@]}" | http_fetch "$dest"; then
    write_sync_stamp "$dest"
else
    die "uniprot: fetch failed"
fi
