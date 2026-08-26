#!/usr/bin/env bash

# ============================================================
# Episteme - PubMed Daily Updates Downloader
# Features:
#   - Tracks last successfully downloaded file
#   - Resumes automatically from last point
#   - Optional: download only a specific range
# ============================================================

set -euo pipefail

BASE_URL="ftp://ftp.ncbi.nlm.nih.gov/pubmed/updatefiles"
OUTPUT_DIR="${1:-./01_raw/pubmed/updates}"
STATE_FILE="$OUTPUT_DIR/.last_downloaded"
LOG_FILE="$OUTPUT_DIR/download_history.log"

# Optional arguments
# Usage examples:
#   ./download_pubmed_updates.sh
#   ./download_pubmed_updates.sh /path/to/updates
#   ./download_pubmed_updates.sh /path/to/updates 1400 1450     # specific range

START_FROM="${2:-}"
END_AT="${3:-}"

mkdir -p "$OUTPUT_DIR"
cd "$OUTPUT_DIR"

echo "=================================================="
echo "PubMed Daily Updates Downloader"
echo "Target: $(pwd)"
echo "=================================================="

# -------- Get full list of available update files --------
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

# -------- Determine where to start --------
if [[ -n "$START_FROM" ]]; then
  # User specified a range
  FIRST_FILE=$(printf "pubmed26n%04d.xml.gz" "$START_FROM")
  echo "→ Manual start requested from: $FIRST_FILE"
else
  if [[ -f "$STATE_FILE" ]]; then
    LAST=$(cat "$STATE_FILE")
    echo "→ Last downloaded file was: $LAST"
    # Find the next file after the last one
    FIRST_FILE=$(grep -A1 "^${LAST}$" all_update_files.txt | tail -n1 || true)
    if [[ -z "$FIRST_FILE" || "$FIRST_FILE" == "$LAST" ]]; then
      echo "→ Already up to date. Nothing new to download."
      exit 0
    fi
  else
    # First run – start from the beginning of updates (after baseline)
    FIRST_FILE=$(head -n1 all_update_files.txt)
    echo "→ No previous state found. Starting from first update file: $FIRST_FILE"
  fi
fi

# -------- Build the list of files to download --------
echo "→ Building download list starting from $FIRST_FILE ..."

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

# Create full URLs
sed "s|^|${BASE_URL}/|" files_to_download.txt > urls_to_download.txt

# -------- Download --------
echo "→ Starting download..."
aria2c -c \
  -x 8 -s 8 -j 6 \
  --max-tries=12 \
  --retry-wait=30 \
  --auto-file-renaming=false \
  --allow-overwrite=false \
  -i urls_to_download.txt

# -------- Update state --------
LAST_DOWNLOADED=$(tail -n1 files_to_download.txt)
echo "$LAST_DOWNLOADED" > "$STATE_FILE"
echo "$(date '+%Y-%m-%d %H:%M:%S')  Downloaded up to $LAST_DOWNLOADED" >> "$LOG_FILE"

echo ""
echo "=================================================="
echo "Update download completed."
echo "Last file recorded: $LAST_DOWNLOADED"
echo "State saved to: $STATE_FILE"
echo "=================================================="
