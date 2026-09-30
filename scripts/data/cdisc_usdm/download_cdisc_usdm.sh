#!/usr/bin/env bash
# Episteme acquisition wrapper: CDISC USDM (Unified Study Definitions Model), as published
# in the public GitHub repo cdisc-org/DDF-RA. Fetches the Deliverables/ folder of the LATEST
# RELEASE (API spec, USDM controlled terminology, implementation guide, CORE rules, UML
# model). Download-only: no serializer.
# Thin shell over scripts/data/_lib/common.sh — GitHub release/commit/tree + size-skip + fetch.
# No MODE positional: Deliverables/ is the only thing fetched.
# Layout: <raw>/cdisc_usdm/<release-tag>/{Deliverables/...,LICENSE,README.md,PROVENANCE.txt};
# folders of older releases are never touched.
# Licence: the repo LICENSE is MIT for code and scripts; the README grants CC-BY-4.0 to
# "content files like documentation and minutes"; the model files are not named in either -
# verify before redistribution. No serializer exists or is planned for cdisc_usdm until CDISC
# confirms a licence for the model files; this source is excluded from any training corpus.
# Pinning: the release tag is resolved to its commit SHA and every raw fetch uses the SHA, so
# one run is internally consistent even if the tag moves.
# Re-fetch: size-skip cannot detect a same-size change under a moved tag; use --force (with
# --reason) to re-fetch.
# Every run, --dry-run included, makes three GitHub API calls (release, commit, tree); they
# count against the unauthenticated 60 requests/hour limit.
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
        -*)          log WARN "download_cdisc_usdm.sh: ignoring $1"; shift ;;
        *)           die "cdisc_usdm: unknown mode '$1' (this source has no modes)" ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR USDM_API_BASE USDM_RAW_BASE USDM_REPO_URL

_api() { # _api PATH -> response body with CR stripped; empty on failure
    curl -fsS --connect-timeout 30 --max-time 120 "$USDM_API_BASE/$1" 2>/dev/null | tr -d '\r'
}

_raw_url() { # _raw_url REL -> raw URL at the pinned SHA, with % space # ? [ ] { } percent-encoded
    local u="${1//\%/%25}"
    u="${u// /%20}"; u="${u//\#/%23}"; u="${u//\?/%3F}"
    # curl treats [ ] { } in URLs as glob syntax; encode them so paths carrying those
    # characters (e.g. CORE rules filenames) are fetched literally.
    u="${u//\[/%5B}"; u="${u//\]/%5D}"; u="${u//\{/%7B}"; u="${u//\}/%7D}"
    printf '%s/%s/%s' "$USDM_RAW_BASE" "$sha" "$u"
}

tag="$(_api releases/latest | grep -m1 -oE '"tag_name": *"[^"]*"' | sed -E 's/^"tag_name": *"(.*)"$/\1/')"
[ -n "$tag" ] || die "cdisc_usdm: cannot resolve the latest release of $USDM_API_BASE (GitHub API rate limit?)"
[[ "$tag" =~ ^v?[0-9]+(\.[0-9]+)*$ ]] || die "cdisc_usdm: refusing unexpected release tag '$tag'"

# The pretty-printed commit JSON carries the commit's own "sha" first (parents/tree come later).
sha="$(_api "commits/$tag" | grep -m1 -oE '"sha": *"[0-9a-f]{40}"' | grep -oE '[0-9a-f]{40}')"
[ -n "$sha" ] || die "cdisc_usdm: cannot resolve the commit of release $tag (GitHub API rate limit?)"

tree="$(_api "git/trees/$sha?recursive=1")"
[ -n "$tree" ] || die "cdisc_usdm: cannot list the tree of release $tag (GitHub API rate limit?)"
if printf '%s\n' "$tree" | grep -qE '"truncated": *true'; then
    die "cdisc_usdm: GitHub truncated the tree listing of release $tag; refusing a partial download"
fi

dest="$(resolve_dest cdisc_usdm "$tag")"

# One JSON object per tree entry (field order not assumed); emit the path of every blob
# under Deliverables/. path/type are tracked per-entry: reset at each object-open line,
# recorded independently as seen, emitted at the matching object-close line so a reordered
# field (or a reordered GitHub response) cannot pair a path with the wrong entry's type.
# Also cleared immediately after that emit, so the OUTER object's own closing "}" (which
# matches the same close pattern) cannot re-emit the last entry's path a second time.
planned=()
while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    _safe_rel "$rel" || { log WARN "cdisc_usdm: rejecting unsafe path '$rel'"; continue; }
    planned+=("$(_raw_url "$rel")"$'\t'"$rel")
done < <(printf '%s\n' "$tree" | awk '
    /^ *\{ *$/ { path=""; type="" }
    /^ *"path":/ { p=$0; sub(/^ *"path": *"/, "", p); sub(/",? *$/, "", p); path=p }
    /^ *"type":/ { t=$0; sub(/^ *"type": *"/, "", t); sub(/",? *$/, "", t); type=t }
    /^ *\},? *$/ { if (type == "blob" && path ~ /^Deliverables\//) print path; path=""; type="" }')
[ "${#planned[@]}" -gt 0 ] || die "cdisc_usdm: no files resolved under Deliverables/ in release $tag"

# --max-files caps the RESOLVED data-file set here, before the --force prune loop.
if [ -n "$MAX_FILES" ]; then
    mapfile -t planned < <(printf '%s\n' "${planned[@]}" | cap_urls "$MAX_FILES")
fi
# The repo LICENSE and README (licence wording) always ride along (not counted against --max-files).
planned+=("$(_raw_url LICENSE)"$'\t'"LICENSE")
planned+=("$(_raw_url README.md)"$'\t'"README.md")

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
    printf '%s\n' "${lines[@]}" | http_fetch "$dest" || die "cdisc_usdm: fetch failed"
else
    log INFO "cdisc_usdm: up to date ($tag)"
fi

if [ "${EPISTEME_DRY_RUN:-0}" != "1" ] && {
    [ "${#lines[@]}" -gt 0 ] || [ ! -f "$dest/PROVENANCE.txt" ] \
        || ! grep -qx "commit_sha: $sha" "$dest/PROVENANCE.txt"
}; then
    mkdir -p "$dest"
    {
        printf 'source_repo: %s\n' "$USDM_REPO_URL"
        printf 'release_tag: %s\n' "$tag"
        printf 'commit_sha: %s\n' "$sha"
        printf 'retrieved_at: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf 'content_licence: LICENSE is MIT for code and scripts; README grants CC-BY-4.0 to content files like documentation and minutes; model files not covered - verify before redistribution\n'
        printf 'corpus_status: excluded - download-only; no serializer exists or is planned for cdisc_usdm until CDISC confirms a licence for the model files; excluded from any training corpus\n'
    } > "$dest/PROVENANCE.txt"
fi
write_sync_stamp "$(resolve_dest cdisc_usdm)"
