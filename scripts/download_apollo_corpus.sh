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

if ! command -v huggingface-cli >/dev/null 2>&1; then
  echo "Error: huggingface-cli not found. Install with:"
  echo "  pip install -U 'huggingface_hub[cli]'"
  exit 1
fi

huggingface-cli download "$REPO_ID" \
  --repo-type dataset \
  --local-dir "$OUTPUT_DIR" \
  --local-dir-use-symlinks False

echo ""
echo "Download complete: $OUTPUT_DIR"
echo "If ApolloCorpus.zip is present, unzip it before running extract."
