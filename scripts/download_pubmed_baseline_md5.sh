#!/usr/bin/env bash

# ============================================================
# Episteme - PubMed Baseline MD5 Downloader
# ============================================================

set -euo pipefail

BASE_URL="ftp://ftp.ncbi.nlm.nih.gov/pubmed/baseline"
OUTPUT_DIR="${1:-./01_raw/pubmed/md5}"

mkdir -p "$OUTPUT_DIR"
cd "$OUTPUT_DIR"

echo "→ Fetching list of baseline MD5 files..."
curl -s --list-only "$BASE_URL/" \
  | grep -E 'pubmed26n[0-9]+\.xml\.gz\.md5$' \
  | sort > pubmed_baseline_md5_files.txt

FILE_COUNT=$(wc -l < pubmed_baseline_md5_files.txt | tr -d ' ')
echo "→ Found $FILE_COUNT MD5 files"

sed "s|^|${BASE_URL}/|" pubmed_baseline_md5_files.txt > pubmed_baseline_md5_urls.txt

echo "→ Starting MD5 download..."
aria2c -c -x 6 -s 6 -j 8 \
  --max-tries=10 \
  --retry-wait=20 \
  --auto-file-renaming=false \
  -i pubmed_baseline_md5_urls.txt

echo "→ MD5 files downloaded to: $(pwd)"
