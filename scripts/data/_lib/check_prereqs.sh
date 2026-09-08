#!/usr/bin/env bash
# Report presence of the acquisition-layer runtime tools. Never fails.
set -uo pipefail
_row() {
    local t="$1" hint="$2" p
    if p="$(command -v "$t" 2>/dev/null)"; then printf '  %-16s %s\n' "$t" "$p"
    else printf '  %-16s MISSING  (%s)\n' "$t" "$hint"; fi
}
echo "acquisition-layer prerequisites:"
_row aria2c        "http/ftp segmented download; apt/brew install aria2"
_row s5cmd         "fast S3; github.com/peak/s5cmd"
_row aws           "S3 fallback; pip install awscli"
_row curl          "always needed (HEAD checks, dry-run, slow fallback)"
_row hf            "HF; pip install -U 'huggingface_hub[cli,hf_transfer]'"
_row huggingface-cli "HF (older name)"
_row shellcheck    "script lint (CI-enforced; local optional)"
