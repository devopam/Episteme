#!/usr/bin/env bash
# Episteme acquisition wrapper: CDISC Biomedical Concepts + SDTM Dataset
# Specializations, as published in the public GitHub repo cdisc-org/COSMoS
# (export/ = CSV/XLSX of the latest versions). Download-only: no serializer.
# Thin shell over scripts/data/_lib/common.sh — GitHub contents listing + size-skip + fetch.
# MODE positional: export (default) | yaml.
# Repository content is CC-BY-4.0, code MIT; PROVENANCE.txt + LICENSE are stored beside the data.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"

MODE="export"
MAX_FILES=""
FORCE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)   export EPISTEME_DRY_RUN=1; shift ;;
        --max-files) MAX_FILES="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --force)     FORCE=1; shift ;;
        --reason)    shift; [ $# -gt 0 ] && shift ;;   # run_pipeline already enforced it
        export|yaml) MODE="$1"; shift ;;
        *)           log WARN "download_cdisc_bc.sh: ignoring $1"; shift ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR COSMOS_API_BASE COSMOS_RAW_BASE COSMOS_REPO_URL

dest="$(resolve_dest cdisc_bc)"

# Pin the whole run to one commit: resolve the SHA first, then list/fetch at that ref.
# The pretty-printed commit JSON carries the commit's own "sha" first (parents/tree come later).
sha="$(curl -fsS --connect-timeout 30 --max-time 120 "$COSMOS_API_BASE/commits/main" 2>/dev/null \
    | grep -m1 -oE '"sha": *"[0-9a-f]{40}"' | grep -oE '[0-9a-f]{40}')"
[ -n "$sha" ] || die "cdisc_bc: cannot resolve the latest commit of $COSMOS_API_BASE (GitHub API rate limit?)"

listing="$(curl -fsS --connect-timeout 30 --max-time 120 "$COSMOS_API_BASE/contents/$MODE?ref=$sha" 2>/dev/null)" \
    || die "cdisc_bc: cannot list $COSMOS_API_BASE/contents/$MODE (GitHub API rate limit?)"

# One JSON object per entry, one field per line; emit `download_url<TAB>MODE/name` for type=file.
planned=()
while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    rel="${entry#*$'\t'}"
    _safe_rel "$rel" || { log WARN "cdisc_bc: rejecting unsafe path '$rel'"; continue; }
    planned+=("$entry")
done < <(printf '%s\n' "$listing" | tr -d '\r' | awk -v mode="$MODE" '
    /^ *"name":/         { n=$0; sub(/^ *"name": *"/, "", n); sub(/",? *$/, "", n) }
    /^ *"download_url":/ { u=$0; sub(/^ *"download_url": *"/, "", u); sub(/",? *$/, "", u); if (u ~ /^null/) u="" }
    /^ *"type":/         { if ($0 ~ /"file"/ && u != "" && n != "") print u "\t" mode "/" n; n=""; u="" }')
[ "${#planned[@]}" -gt 0 ] || die "cdisc_bc: no files resolved under $COSMOS_API_BASE/contents/$MODE (mode=$MODE)"

# --max-files caps the RESOLVED data-file set here, before the --force prune loop.
if [ -n "$MAX_FILES" ]; then
    mapfile -t planned < <(printf '%s\n' "${planned[@]}" | cap_urls "$MAX_FILES")
fi
# The repo LICENSE always rides along (not counted against --max-files).
planned+=("$COSMOS_RAW_BASE/$sha/LICENSE"$'\t'"LICENSE")

lines=()
for entry in "${planned[@]}"; do
    url="${entry%%$'\t'*}"
    rel="${entry#*$'\t'}"
    # never delete real files during a --dry-run preview
    if [ "$FORCE" = "1" ] && [ "${EPISTEME_DRY_RUN:-0}" != "1" ]; then rm -f "$dest/$rel"; fi
    if size_match_skip "$dest/$rel" "$url"; then
        log INFO "skip (size-matched): $rel"
    else
        lines+=("$entry")
    fi
done

if [ "${#lines[@]}" -gt 0 ]; then
    printf '%s\n' "${lines[@]}" | http_fetch "$dest" || die "cdisc_bc: fetch failed"
else
    log INFO "cdisc_bc: up to date"
fi

if [ "${EPISTEME_DRY_RUN:-0}" != "1" ]; then
    mkdir -p "$dest"
    {
        printf 'source_repo: %s\n' "$COSMOS_REPO_URL"
        printf 'commit_sha: %s\n' "$sha"
        printf 'retrieved_at: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf 'content_licence: CC-BY-4.0 (repository content); MIT (code)\n'
    } > "$dest/PROVENANCE.txt"
fi
write_sync_stamp "$dest"
