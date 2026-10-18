#!/usr/bin/env bash
# hf_fetch REPO_ID REPO_TYPE DEST_DIR [REVISION]  -- Hugging Face download helper.
# Sourced by wrappers; match common.sh — only `pipefail` is safe to inherit,
# `-e` / `-u` stay the caller's choice (do not set `-u` here: it would leak into
# the sourcing wrapper and abort it on any unguarded ${VAR}).
set -o pipefail

# The CLI prints this, with exit 0, when it cannot read the repo (a gated repo
# without a token, a wrong repo type) and returns the existing --local-dir as if
# it were the snapshot. That is a failure, not a download.
_HF_NO_REPO_MSG="as remote repo cannot be accessed"

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
    # The hub's 10 s defaults time out on a busy link (2026-10-18: apollo and
    # guidelines both failed so); the caller's own values win.
    export HF_HUB_DOWNLOAD_TIMEOUT="${HF_HUB_DOWNLOAD_TIMEOUT:-60}"
    export HF_HUB_ETAG_TIMEOUT="${HF_HUB_ETAG_TIMEOUT:-60}"

    local -a args=(download "$repo" --local-dir "$dest")
    [ "$rtype" != "model" ] && args+=(--repo-type "$rtype")
    [ -n "$rev" ] && args+=(--revision "$rev")

    # No fallback to another repo type: each wrapper pins its repo and type, and the
    # old retry "as model" ended in the no-repo message above with exit 0, so a
    # failed download reported success. A failed call is retried as it is; the CLI
    # resumes the files it already has.
    local tries="${EPISTEME_HF_TRIES:-3}" delay="${EPISTEME_HF_RETRY_DELAY:-30}"
    local errf attempt rc
    errf="$(mktemp)"
    for ((attempt = 1; attempt <= tries; attempt++)); do
        "$bin" "${args[@]}" 2> >(tee "$errf" >&2)
        rc=$?
        wait # let tee finish writing $errf
        if grep -q "$_HF_NO_REPO_MSG" "$errf"; then
            echo "ERROR: $bin cannot read $repo (type=$rtype): a gated repo needs HF_TOKEN and accepted terms" >&2
            rm -f "$errf"
            return 1
        fi
        if [ "$rc" -eq 0 ]; then
            rm -f "$errf"
            return 0
        fi
        echo "WARN: $bin download $repo failed (rc=$rc, attempt $attempt of $tries)" >&2
        [ "$attempt" -lt "$tries" ] && sleep "$((delay * attempt))"
    done
    rm -f "$errf"
    return 1
}
