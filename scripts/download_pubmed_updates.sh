#!/usr/bin/env bash

# ============================================================
# Episteme - PubMed Daily Updates Downloader
# Downloads both .xml.gz and corresponding .md5 files
# Features:
#   - Tracks last successfully downloaded file
#   - Resumes automatically from last point
#   - Optional: download only a specific range
# ============================================================

set -euo pipefail

BASE_URL="ftp://ftp.ncbi.nlm.nih.gov/pubmed/updatefiles"
OUTPUT_DIR="${1:-./01_raw/pubmed/updates}"
MD5_DIR="${OUTPUT_DIR}/../md5"                  # Store md5 files together with baseline md5s
STATE_FILE="$OUTPUT_DIR/.last_downloaded"
LOG_FILE="$OUTPUT_DIR/download_history.log"

# Optional range arguments
START_FROM="${2:-}"
END_AT="${3:-}"

mkdir -p "$OUTPUT_DIR"
mkdir -p "$MD5_DIR"
cd "$OUTPUT_DIR"

echo "=================================================="
echo "PubMed Daily Updates Downloader"
echo "XML target : $(pwd)"
echo "MD5 target : $MD5_DIR"
echo "=================================================="

# -------- Get list of available update files --------
echo "→ Fetching list of available update files..."
curl -s --list-only "$BASE_URL/" \
  | grep -E 'pubmed26n[0-9]+\.xml\.gz$' \
  | sort > all_update_files.txt

TOTAL=$(wc -l < all_update_files.txt | tr -d ' ')
echo "→ Total update files available: $TOTAL"

if [[ "$TOTAL" -eq 0 ]]; then
  echo "ERROR: No update files found."
  exit 1
fi

# -------- Determine starting point --------
if [[ -n "$START_FROM" ]]; then
  FIRST_FILE=$(printf "pubmed26n%04d.xml.gz" "$START_FROM")
  echo "→ Manual start requested from: $FIRST_FILE"
else
  if [[ -f "$STATE_FILE" ]]; then
    LAST=$(cat "$STATE_FILE")
    echo "→ Last downloaded file was: $LAST"
    FIRST_FILE=$(grep -A1 "^${LAST}$" all_update_files.txt | tail -n1 || true)
    if [[ -z "$FIRST_FILE" || "$FIRST_FILE" == "$LAST" ]]; then
      echo "→ Already up to date. Nothing new to download."
      exit 0
    fi
  else
    FIRST_FILE=$(head -n1 all_update_files.txt)
    echo "→ No previous state found. Starting from: $FIRST_FILE"
  fi
fi

# -------- Build list of files to download --------
if [[ -n "$END_AT" ]]; then
  LAST_FILE=$(printf "pubmed26n%04d.xml.gz" "$END_AT")
  sed -n "/^${FIRST_FILE}$/,/^${LAST_FILE}$/p" all_update_files.txt > files_to_download.txt
else
  sed -n "/^${FIRST_FILE}$/,\$p" all_update_files.txt > files_to_download.txt
fi

COUNT=$(wc -l < files_to_download.txt | tr -d ' ')
echo "→ Files queued for download: $COUNT"

if [[ "$COUNT" -eq 0 ]]; then
  echo "→ Nothing to download."
  exit 0
fi

# Create URL lists for both XML and MD5
sed "s|^|${BASE_URL}/|" files_to_download.txt > xml_urls.txt
sed "s|^|${BASE_URL}/|; s|$|.md5|" files_to_download.txt > md5_urls.txt

# -------- Download XML files --------
echo ""
echo "→ Downloading XML files..."
aria2c -c \
  -x 8 -s 8 -j 6 \
  --max-tries=12 \
  --retry-wait=30 \
  --auto-file-renaming=false \
  --allow-overwrite=false \
  -i xml_urls.txt

# -------- Download corresponding MD5 files --------
echo ""
echo "→ Downloading corresponding MD5 files..."
aria2c -c \
  -x 6 -s 6 -j 8 \
  --max-tries=10 \
  --retry-wait=20 \
  --auto-file-renaming=false \
  --allow-overwrite=false \
  --dir="$MD5_DIR" \
  -i md5_urls.txt

# -------- Update state --------
LAST_DOWNLOADED=$(tail -n1 files_to_download.txt)
echo "$LAST_DOWNLOADED" > "$STATE_FILE"
echo "$(date '+%Y-%m-%d %H:%M:%S')  Downloaded up to $LAST_DOWNLOADED" >> "$LOG_FILE"

echo ""
echo "=================================================="
echo "Update download completed."
echo "Last file recorded : $LAST_DOWNLOADED"
echo "XML files location : $OUTPUT_DIR"
echo "MD5 files location : $MD5_DIR"
echo "=================================================="
