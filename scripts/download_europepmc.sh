#!/usr/bin/env bash

# ============================================================
# Episteme - Europe PMC Preprints & Abstracts Incremental Downloader
# Designed for Linux & macOS portability
# ============================================================

set -euo pipefail

# Configuration defaults
FTP_HOST="ftp://ftp.ebi.ac.uk"
PREPRINTS_PATH="/pub/databases/pmc/preprints/"
ABSTRACTS_PATH="/pub/databases/pmc/preprint_abstracts/"

OUTPUT_ROOT="${1:-./01_raw/europepmc}"
SUBSET="${2:-preprints}"                    # Options: preprints, abstracts, both

CONNECTIONS_PER_SERVER=8
SPLITS=8
MAX_CONCURRENT_DOWNLOADS=4
MAX_TRIES=10
RETRY_WAIT=30

echo "=================================================="
mkdir -p "$OUTPUT_ROOT"
ABS_OUTPUT_ROOT=$(cd "$OUTPUT_ROOT" && pwd)
echo "Europe PMC Incremental Downloader"
echo "Target root directory: $ABS_OUTPUT_ROOT"
echo "Target subset        : $SUBSET"
echo "=================================================="

# Helper function to get local file size in bytes (macOS & Linux compatible)
get_local_file_size() {
  local file="$1"
  if [[ -f "$file" ]]; then
    wc -c < "$file" | tr -d '[:space:]'
  else
    echo "0"
  fi
}

# Helper function to get remote file size in bytes from EBI FTP listing
get_remote_file_size() {
  local parent_url="$1"
  local file_name="$2"
  curl -s "$parent_url" | grep -F "$file_name" | awk '{print $5}' | tr -d '[:space:]'
}

download_preprints() {
  local target_dir="$ABS_OUTPUT_ROOT/preprints"
  mkdir -p "$target_dir"
  
  echo ""
  echo "→ Syncing preprints ..."
  
  # Fetch list of preprint files
  local files
  files=$(curl -s --list-only "${FTP_HOST}${PREPRINTS_PATH}" | grep -E '\.xml\.gz$' || true)
  
  if [[ -z "$files" ]]; then
    echo "ERROR: No preprint XML files found at ${FTP_HOST}${PREPRINTS_PATH}"
    exit 1
  fi
  
  # Create list of URLs to download
  local url_list_file="$target_dir/urls_to_download.txt"
  rm -f "$url_list_file"
  
  echo "$files" | while read -r file_name; do
    if [[ -z "$file_name" ]]; then continue; fi
    
    local local_file="$target_dir/$file_name"
    local remote_url="${FTP_HOST}${PREPRINTS_PATH}$file_name"
    
    # Static ranges: skip if already present
    if [[ -f "$local_file" ]]; then
      echo "  [Skip] $file_name (already downloaded)"
    else
      echo "$remote_url" >> "$url_list_file"
    fi
  done
  
  if [[ -f "$url_list_file" ]]; then
    echo "→ Downloading preprints using aria2c..."
    (
      cd "$target_dir"
      aria2c -c \
        -x "$CONNECTIONS_PER_SERVER" \
        -s "$SPLITS" \
        -j "$MAX_CONCURRENT_DOWNLOADS" \
        --max-tries="$MAX_TRIES" \
        --retry-wait="$RETRY_WAIT" \
        --auto-file-renaming=false \
        --allow-overwrite=false \
        -i "$url_list_file"
    )
    rm -f "$url_list_file"
  else
    echo "→ All preprint range files are already up-to-date locally."
  fi
}

download_abstracts() {
  local target_dir="$ABS_OUTPUT_ROOT/preprint_abstracts"
  mkdir -p "$target_dir"
  
  echo ""
  echo "→ Syncing preprint abstracts ..."
  
  local files
  files=$(curl -s --list-only "${FTP_HOST}${ABSTRACTS_PATH}" | grep -E '\.zip$' || true)
  
  if [[ -z "$files" ]]; then
    echo "ERROR: No abstract zip files found at ${FTP_HOST}${ABSTRACTS_PATH}"
    exit 1
  fi
  
  echo "$files" | while read -r file_name; do
    if [[ -z "$file_name" ]]; then continue; fi
    
    local local_file="$target_dir/$file_name"
    local remote_url="${FTP_HOST}${ABSTRACTS_PATH}$file_name"
    
    local remote_size
    remote_size=$(get_remote_file_size "${FTP_HOST}${ABSTRACTS_PATH}" "$file_name")
    
    local local_size
    local_size=$(get_local_file_size "$local_file")
    
    if [[ "$local_size" -eq 0 ]]; then
      echo "  [New] Downloading $file_name ($((remote_size / 1024 / 1024)) MB) in full..."
      (
        cd "$target_dir"
        aria2c -c -x "$CONNECTIONS_PER_SERVER" -s "$SPLITS" --max-tries="$MAX_TRIES" --retry-wait="$RETRY_WAIT" "$remote_url"
      )
    elif [[ "$local_size" -ne "$remote_size" ]]; then
      echo "  [Update] Local file size ($local_size) differs from remote ($remote_size)."
      echo "           Redownloading $file_name to prevent zip corruption..."
      rm -f "$local_file"
      (
        cd "$target_dir"
        aria2c -c -x "$CONNECTIONS_PER_SERVER" -s "$SPLITS" --max-tries="$MAX_TRIES" --retry-wait="$RETRY_WAIT" "$remote_url"
      )
    else
      echo "  [Skip] $file_name (up-to-date, size matches: $local_size bytes)"
    fi
  done
}

# Run selection
if [[ "$SUBSET" == "preprints" ]]; then
  download_preprints
elif [[ "$SUBSET" == "abstracts" ]]; then
  download_abstracts
elif [[ "$SUBSET" == "both" ]]; then
  download_preprints
  download_abstracts
else
  echo "ERROR: Invalid subset option '$SUBSET'. Use 'preprints', 'abstracts', or 'both'."
  exit 1
fi

echo ""
echo "=================================================="
echo "Europe PMC sync completed."
echo "=================================================="
