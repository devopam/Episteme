#!/usr/bin/env bash
# Episteme SP3 acquisition wrapper: EPFL-LLM clinical guidelines corpus
# (Hugging Face dataset, repo pinned via GUIDELINES_HF_REPO in
# scripts/data/_lib/sources.env). Thin shell over scripts/data/_lib/common.sh
# + scripts/data/_lib/hf_download.sh — delegates to `hf download`, which does
# its own resume (no extract/parse). No positional.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"
# shellcheck source=../_lib/hf_download.sh
. "$HERE/../_lib/hf_download.sh"

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)   export EPISTEME_DRY_RUN=1; shift ;;
        --max-files) log INFO "guidelines: --max-files ignored (hf download does its own resume)"; shift; [ $# -gt 0 ] && shift ;;
        --force)     log INFO "guidelines: --force ignored (hf download does its own resume)"; shift ;;
        --reason)    shift; [ $# -gt 0 ] && shift ;;   # run_pipeline already enforced it
        *)           log WARN "download_guidelines.sh: ignoring $1"; shift ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR GUIDELINES_HF_REPO

dest="$(resolve_dest guidelines)"

if hf_fetch "$GUIDELINES_HF_REPO" dataset "$dest"; then
    if [ "${EPISTEME_DRY_RUN:-0}" != "1" ]; then
        printf '%s\n' "$GUIDELINES_HF_REPO" > "$dest/hf_repo_id.txt"
    fi
    write_sync_stamp "$dest"
else
    die "guidelines: hf_fetch failed for $GUIDELINES_HF_REPO"
fi
