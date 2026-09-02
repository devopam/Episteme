#!/usr/bin/env bash
# ============================================================
# Episteme – NCBI Bookshelf / NLM LitArch Open Access subset
# FULL download via official FTP only (restartable)
#
# Official bulk path (do NOT scrape bookshelf HTML):
#   https://ftp.ncbi.nlm.nih.gov/pub/litarch/
#   file_list.txt / file_list.csv list every OA book tar.gz
#
# Usage:
#   ./download_bookshelf_oa.sh [output_dir] [limit]
#     limit=0  → all OA books (default)
#     limit=N  → first N archives (smoke / laptop)
# ============================================================
set -euo pipefail

OUTPUT_DIR="${1:-./01_raw/bookshelf_oa}"
LIMIT="${2:-0}"

BASE="https://ftp.ncbi.nlm.nih.gov/pub/litarch"
CONNECTIONS=8
SPLITS=8
JOBS=4
MAX_TRIES=15
RETRY_WAIT=30

echo "=================================================="
echo "NCBI Bookshelf OA (LitArch) Downloader"
echo "Target : $OUTPUT_DIR"
echo "Limit  : $LIMIT (0=all)"
echo "=================================================="

command -v aria2c >/dev/null 2>&1 || { echo "ERROR: aria2c required" >&2; exit 1; }
command -v curl   >/dev/null 2>&1 || { echo "ERROR: curl required" >&2; exit 1; }

mkdir -p "$OUTPUT_DIR/packages"
ABS_OUT="$(cd "$OUTPUT_DIR" && pwd)"

# File list
curl -sS -L --fail --connect-timeout 30 --max-time 180 \
  "$BASE/file_list.txt" -o "$ABS_OUT/file_list.txt"
curl -sS -L --fail --connect-timeout 30 --max-time 180 \
  "$BASE/file_list.csv" -o "$ABS_OUT/file_list.csv" || true

# file_list.txt lines typically: path/to/book.tar.gz<TAB>metadata...
# Extract first field paths ending in .tar.gz
mapfile_compat() {
  # portable: no mapfile
  grep -oE '[0-9a-fA-F/_.-]+\.tar\.gz' "$ABS_OUT/file_list.txt" 2>/dev/null \
    | sort -u \
    || awk -F'\t' 'NF{print $1}' "$ABS_OUT/file_list.txt" | grep '\.tar\.gz$' | sort -u
}

PACKAGES="$(mapfile_compat)"
if [[ -z "$PACKAGES" ]]; then
  # CSV fallback: first column
  PACKAGES="$(
    awk -F',' 'NR>1{
      gsub(/"/,"",$1);
      if ($1 ~ /\.tar\.gz$/) print $1
    }' "$ABS_OUT/file_list.csv" 2>/dev/null | sort -u
  )"
fi

if [[ -z "$PACKAGES" ]]; then
  echo "ERROR: could not parse package list from file_list.txt/csv" >&2
  exit 1
fi

total="$(printf '%s\n' "$PACKAGES" | grep -c . || true)"
echo "→ Packages listed: $total"

if [[ "$LIMIT" =~ ^[0-9]+$ ]] && [[ "$LIMIT" -gt 0 ]]; then
  PACKAGES="$(printf '%s\n' "$PACKAGES" | head -n "$LIMIT")"
  echo "→ Limited to: $LIMIT"
fi

URL_LIST="$ABS_OUT/urls_to_download.txt"
rm -f "$URL_LIST"
need=0
skip=0

while IFS= read -r rel; do
  [[ -z "$rel" ]] && continue
  rel="${rel#./}"
  fname="$(basename "$rel")"
  # Keep shallow local layout under packages/
  local_path="$ABS_OUT/packages/$fname"
  remote="$BASE/$rel"

  if [[ -f "$local_path" && -s "$local_path" ]]; then
    remote_len="$(curl -sS -I -L --connect-timeout 20 --max-time 60 "$remote" 2>/dev/null \
      | tr -d '\r' | awk 'tolower($1)=="content-length:"{print $2; exit}')"
    local_len="$(wc -c < "$local_path" | tr -d '[:space:]')"
    if [[ -n "$remote_len" && "$remote_len" =~ ^[0-9]+$ && "$local_len" -eq "$remote_len" ]]; then
      echo "  [Skip] $fname"
      skip=$((skip + 1))
      continue
    fi
    echo "  [Update] $fname"
    rm -f "$local_path"
  else
    echo "  [New] $fname"
  fi
  {
    echo "$remote"
    echo "  dir=$ABS_OUT/packages"
    echo "  out=$fname"
  } >> "$URL_LIST"
  need=$((need + 1))
done <<< "$PACKAGES"

echo "  Summary: skip=$skip download=$need"
printf '%s\n' "$PACKAGES" > "$ABS_OUT/remote_manifest.txt"

if [[ -s "$URL_LIST" ]]; then
  aria2c -c -x "$CONNECTIONS" -s "$SPLITS" -j "$JOBS" \
    --max-tries="$MAX_TRIES" --retry-wait="$RETRY_WAIT" \
    --auto-file-renaming=false --allow-overwrite=true \
    --file-allocation=none \
    -i "$URL_LIST"
  rm -f "$URL_LIST"
else
  echo "→ Nothing new to download."
fi

date -u +"%Y-%m-%dT%H:%M:%SZ" > "$ABS_OUT/last_sync_utc.txt"
echo "=================================================="
echo "Bookshelf OA sync complete → $ABS_OUT"
echo "Only LitArch OA subset via FTP (HTML scrape is prohibited)."
echo "Per-book licenses still vary — filter at extract time."
echo "=================================================="
