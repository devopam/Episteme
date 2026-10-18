#!/usr/bin/env bash
# hf_fetch REPO_ID REPO_TYPE DEST_DIR [REVISION]  -- Hugging Face download helper.
# Sourced by wrappers; match common.sh — only `pipefail` is safe to inherit,
# `-e` / `-u` stay the caller's choice (do not set `-u` here: it would leak into
# the sourcing wrapper and abort it on any unguarded ${VAR}).
set -o pipefail

# huggingface_hub's snapshot_download logs this, and the CLI exits 0, when its
# repo_info call fails and --local-dir is not empty: it returns the old folder as
# if it were the snapshot. That is never a download. The call fails on a timeout,
# a lost connection or a hub 5xx (worth a retry) and on 401 / 403 / 404 (a gated
# or private repo without access, a wrong repo or type: no retry helps).
_HF_NO_REPO_MSG="as remote repo cannot be accessed"
_HF_NO_ACCESS='(401|403|404) Client Error|Repository Not Found|gated'

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
    # The CLI's stderr goes to a file that is read after each attempt; if the file
    # cannot be made or written, the attempt fails rather than skip the check.
    errf="$(mktemp)" || { echo "ERROR: hf_fetch: mktemp failed" >&2; return 1; }
    for ((attempt = 1; attempt <= tries; attempt++)); do
        # in a condition, so a caller's `set -e` cannot end the loop early
        if "$bin" "${args[@]}" 2>"$errf"; then rc=0; else rc=$?; fi
        cat "$errf" >&2
        if [ "$rc" -eq 0 ] && ! grep -q "$_HF_NO_REPO_MSG" "$errf"; then
            rm -f "$errf"
            return 0
        fi
        if grep -q "$_HF_NO_REPO_MSG" "$errf" && grep -Eq "$_HF_NO_ACCESS" "$errf"; then
            echo "ERROR: $bin cannot read $repo (type=$rtype): no access (a gated repo needs HF_TOKEN and accepted terms) or no such repo" >&2
            rm -f "$errf"
            return 1
        fi
        echo "WARN: $bin download $repo failed (rc=$rc, attempt $attempt of $tries)" >&2
        if [ "$attempt" -lt "$tries" ]; then sleep "$((delay * attempt))"; fi
    done
    rm -f "$errf"
    return 1
}
