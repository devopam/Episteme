#!/usr/bin/env bash

# ============================================================
# Episteme - PubMed Baseline Downloader (aria2c)
# ============================================================

set -euo pipefail

# -------- Configuration --------
BASE_URL="ftp://ftp.ncbi.nlm.nih.gov/pubmed/baseline"
OUTPUT_DIR="${1:-./01_raw/pubmed/baseline}"   # You can pass a custom path as first argument
CONNECTIONS_PER_SERVER=8
SPLITS=8
MAX_CONCURRENT_DOWNLOADS=6
MAX_TRIES=12
RETRY_WAIT=30

# -------- Prepare directories --------
mkdir -p "$OUTPUT_DIR"
cd "$OUTPUT_DIR"

echo "=================================================="
echo "PubMed Baseline Downloader"
echo "Target directory: $(pwd)"
echo "=================================================="

# -------- Step 1: Get list of baseline files --------
echo "→ Fetching list of baseline files..."
curl -s --list-only "$BASE_URL/" \
  | grep -E 'pubmed26n[0-9]+\.xml\.gz$' \
  | sort > pubmed_baseline_files.txt

FILE_COUNT=$(wc -l < pubmed_baseline_files.txt | tr -d ' ')
echo "→ Found $FILE_COUNT baseline files"

if [[ "$FILE_COUNT" -eq 0 ]]; then
  echo "ERROR: No baseline files found. Exiting."
  exit 1
fi

# -------- Step 2: Create full URL list --------
echo "→ Creating URL list..."
sed "s|^|${BASE_URL}/|" pubmed_baseline_files.txt > pubmed_baseline_urls.txt

# -------- Step 3: Download with aria2c --------
echo "→ Starting download with aria2c..."
echo "   (This is resumable – you can safely stop and re-run the script)"
echo ""

aria2c -c \
  -x "$CONNECTIONS_PER_SERVER" \
  -s "$SPLITS" \
  -j "$MAX_CONCURRENT_DOWNLOADS" \
  --max-tries="$MAX_TRIES" \
  --retry-wait="$RETRY_WAIT" \
  --auto-file-renaming=false \
  --allow-overwrite=false \
  -i pubmed_baseline_urls.txt

echo ""
echo "=================================================="
echo "Download finished (or caught up)."
echo "Files are in: $(pwd)"
echo "=================================================="
