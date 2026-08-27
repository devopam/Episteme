#!/usr/bin/env bash
# Episteme – Download ApolloCorpus (FreedomIntelligence)
# License: Apache-2.0 (commercial use allowed)

set -euo pipefail

REPO_ID="FreedomIntelligence/ApolloCorpus"
OUTPUT_DIR="${1:-./01_raw/multilingual/apollo/raw}"

echo "=================================================="
echo "ApolloCorpus Downloader"
echo "Repo: $REPO_ID"
echo "Target: $OUTPUT_DIR"
echo "=================================================="

mkdir -p "$OUTPUT_DIR"

# Requires: pip install -U "huggingface_hub[cli]"
huggingface-cli download "$REPO_ID" \
  --repo-type dataset \
  --local-dir "$OUTPUT_DIR" \
  --local-dir-use-symlinks False

echo ""
echo "Download finished."
echo "Data location: $OUTPUT_DIR"
echo "Next: run the extraction script to produce JSONL / Parquet."
