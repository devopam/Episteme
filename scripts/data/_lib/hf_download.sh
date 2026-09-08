#!/usr/bin/env bash
# hf_fetch REPO_ID REPO_TYPE DEST_DIR [REVISION]  -- Hugging Face download helper.
# Sourced by wrappers; match common.sh — only `pipefail` is safe to inherit,
# `-e` / `-u` stay the caller's choice (do not set `-u` here: it would leak into
# the sourcing wrapper and abort it on any unguarded ${VAR}).
set -o pipefail

hf_fetch() {
    local repo="$1" rtype="${2:-dataset}" dest="$3" rev="${4:-}"
    local bin=""
    command -v hf >/dev/null 2>&1 && bin="hf"
    [ -z "$bin" ] && command -v huggingface-cli >/dev/null 2>&1 && bin="huggingface-cli"
    [ -z "$bin" ] && { echo "ERROR: install the HF CLI (pip install -U 'huggingface_hub[cli,hf_transfer]')" >&2; return 1; }

    if [ "${EPISTEME_DRY_RUN:-0}" = "1" ]; then
        echo "DRY: would $bin download $repo (type=$rtype${rev:+ rev=$rev}) -> $dest" >&2
        return 0
    fi
    mkdir -p "$dest"
    # Only opt into the Rust accelerator if hf_transfer is actually importable —
    # otherwise the CLI raises ValueError and the fetch fails for no good reason.
    if { command -v python >/dev/null 2>&1 && python -c 'import hf_transfer' >/dev/null 2>&1; } \
       || { command -v python3 >/dev/null 2>&1 && python3 -c 'import hf_transfer' >/dev/null 2>&1; }; then
        export HF_HUB_ENABLE_HF_TRANSFER=1
    fi
    local -a args=(download "$repo" --local-dir "$dest")
    [ "$rtype" != "model" ] && args+=(--repo-type "$rtype")
    [ -n "$rev" ] && args+=(--revision "$rev")
    if "$bin" "${args[@]}"; then return 0; fi
    if [ "$rtype" = "model" ]; then
        return 1   # already the no-type form; nothing left to fall back to
    fi
    # retry as model-type: drop --repo-type (model is the HF CLI default). Batch-
    # script behaviour — some repos are published as model, not dataset.
    echo "WARN: $bin download as $rtype failed; retrying as model" >&2
    args=(download "$repo" --local-dir "$dest"); [ -n "$rev" ] && args+=(--revision "$rev")
    "$bin" "${args[@]}"
}
