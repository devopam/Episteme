#!/usr/bin/env bash
# Episteme SP3 acquisition wrapper: OpenAlex snapshot sync. Thin shell over
# scripts/data/_lib/common.sh — S3 prefix sync only (no extract/parse). The
# bucket URI comes from $OPENALEX_S3 (scripts/data/_lib/sources.env).
# MODE positional: works_jsonl (default) | works_parquet | jsonl | parquet | full.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"

MODE="works_jsonl"
MAX_FILES=""

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)   export EPISTEME_DRY_RUN=1; shift ;;
        --max-files) MAX_FILES="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --force)     shift ;;                          # no local prune here — consume + ignore
        --reason)    shift; [ $# -gt 0 ] && shift ;;   # run_pipeline already enforced it
        works_jsonl|works_parquet|jsonl|parquet|full) MODE="$1"; shift ;;
        *)           log WARN "download_openalex.sh: ignoring $1"; shift ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR OPENALEX_S3

# MODE -> S3 suffix (relative to $OPENALEX_S3) AND dest subpath (same string).
case "$MODE" in
    works_jsonl)   suffix="data/jsonl/works" ;;
    works_parquet) suffix="data/parquet/works" ;;
    jsonl)         suffix="data/jsonl" ;;
    parquet)       suffix="data/parquet" ;;
    full)          suffix="" ;;
    *)             die "openalex: bad mode '$MODE' (works_jsonl|works_parquet|jsonl|parquet|full)" ;;
esac

[ -z "$MAX_FILES" ] || log INFO "openalex: --max-files is ignored for an S3 prefix sync"

src="${OPENALEX_S3%/}${suffix:+/$suffix}"
dst="$(resolve_dest openalex "$suffix")"

if s3_sync "$src" "$dst"; then
    if [ "${EPISTEME_DRY_RUN:-0}" != "1" ]; then
        printf '%s\n' "$MODE" > "$dst/sync_mode.txt"
    fi
    write_sync_stamp "$dst"
else
    die "openalex: sync failed"
fi
