#!/usr/bin/env bash
# Episteme SP3 acquisition wrapper: UniProtKB Swiss-Prot bulk download.
# Thin shell over scripts/data/_lib/common.sh — mirror probe + size-skip + fetch
# only (no extract/parse). Swiss-Prot product set only; TrEMBL is not fetched.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/data/_lib/common.sh
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
require_env EPISTEME_ACTOR

dest="$(resolve_dest uniprot swissprot)"

# W-3: pick the first mirror that serves reldate.txt with a 200. Runs even under
# --dry-run (cheap) so the chosen mirror is always printed.
BASE=""
code=""
IFS='|' read -r -a mirrors <<< "${UNIPROT_MIRRORS:-}"
for m in "${mirrors[@]}"; do
    code="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 20 --max-time 45 "$m/reldate.txt" 2>/dev/null || true)"
    [ "$code" = "200" ] && { BASE="$m"; break; }
done
[ -n "$BASE" ] || die "uniprot: no mirror reachable"
log INFO "uniprot mirror: $BASE"

# U-1: ancillaries first so --max-files 2 grabs the two cheap files.
files=(reldate.txt LICENSE README uniprot.xsd uniprot_sprot.xml.gz uniprot_sprot.fasta.gz uniprot_sprot.dat.gz uniprot_sprot_varsplic.fasta.gz)

urls=()
for f in "${files[@]}"; do
    u="$BASE/$f"
    [ "$FORCE" = "1" ] && rm -f "$dest/$f"          # W-4: force a re-fetch
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

if printf '%s\n' "${urls[@]}" | cap_urls "$MAX_FILES" | http_fetch "$dest"; then
    write_sync_stamp "$dest"
else
    die "uniprot: fetch failed"
fi
