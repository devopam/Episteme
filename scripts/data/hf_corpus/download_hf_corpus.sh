#!/usr/bin/env bash
# Episteme SP3 acquisition wrapper: generic Hugging Face repo fetch. Thin shell
# over scripts/data/_lib/common.sh + scripts/data/_lib/hf_download.sh — delegates
# to `hf download`, which does its own resume (no extract/parse). The repo id is
# an argument, not an endpoint var.
# Usage: download_hf_corpus.sh [flags] <repo_id> [output_subdir] [revision]
#        flags: --repo-type dataset|model   (default dataset)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib/common.sh
. "$HERE/../_lib/common.sh"
# shellcheck source=../_lib/hf_download.sh
. "$HERE/../_lib/hf_download.sh"

REPO_ID=""
OUT_SUBDIR=""
REVISION=""
REPO_TYPE="dataset"

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)   export EPISTEME_DRY_RUN=1; shift ;;
        --repo-type) REPO_TYPE="${2:-dataset}"; shift; [ $# -gt 0 ] && shift ;;
        --max-files) log INFO "hf_corpus: --max-files ignored (hf download does its own resume)"; shift; [ $# -gt 0 ] && shift ;;
        --force)     log INFO "hf_corpus: --force ignored (hf download does its own resume)"; shift ;;
        --reason)    shift; [ $# -gt 0 ] && shift ;;   # run_pipeline already enforced it
        *)
            if   [ -z "$REPO_ID" ];    then REPO_ID="$1"
            elif [ -z "$OUT_SUBDIR" ]; then OUT_SUBDIR="$1"
            elif [ -z "$REVISION" ];   then REVISION="$1"
            else log WARN "download_hf_corpus.sh: ignoring $1"
            fi
            shift ;;
    esac
done

load_dotenv
require_env EPISTEME_ACTOR

# Ruling HC-1: no repo_id is a soft no-op under a dry run (Task 11's sweep runs
# `run_pipeline.sh hf_corpus download --dry-run` with no repo_id, needs rc 0);
# a real run with no repo_id is an error.
if [ -z "$REPO_ID" ]; then
    if [ "${EPISTEME_DRY_RUN:-0}" = "1" ]; then
        log WARN "hf_corpus: needs a <repo_id> arg (e.g. run_pipeline.sh hf_corpus download <repo_id>)"
        exit 0
    fi
    die "hf_corpus: <repo_id> required"
fi

dest="$(resolve_dest hf_corpus "${OUT_SUBDIR:-${REPO_ID//\//_}}")"

if hf_fetch "$REPO_ID" "${REPO_TYPE:-dataset}" "$dest" "${REVISION:-}"; then
    if [ "${EPISTEME_DRY_RUN:-0}" != "1" ]; then
        printf '%s\n' "$REPO_ID" > "$dest/hf_repo_id.txt"
    fi
    write_sync_stamp "$dest"
else
    die "hf_corpus: hf_fetch failed for $REPO_ID"
fi
