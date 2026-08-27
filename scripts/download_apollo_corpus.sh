#!/usr/bin/env bash
# Episteme – Download ApolloCorpus
set -euo pipefail

REPO_ID="FreedomIntelligence/ApolloCorpus"
OUTPUT_DIR="${1:-./01_raw/multilingual/apollo/raw}"

echo "=================================================="
echo "ApolloCorpus Downloader"
echo "Repo   : $REPO_ID"
echo "Target : $OUTPUT_DIR"
echo "=================================================="

mkdir -p "$OUTPUT_DIR"

if ! command -v hf >/dev/null 2>&1; then
  echo "Error: 'hf' command not found."
  echo "Install with one of:"
  echo "  brew install huggingface-cli"
  echo "  # or: pipx install 'huggingface_hub[cli]'"
  exit 1
fi

hf download "$REPO_ID" \
  --repo-type dataset \
  --local-dir "$OUTPUT_DIR"

echo ""
echo "Download complete: $OUTPUT_DIR"
echo "Extract script will unzip automatically if needed."
