#!/usr/bin/env bash
# ============================================================
# Episteme – NCBI Bookshelf / NLM LitArch Open Access subset
# Restartable full download via official FTP only.
#
# Usage:
#   ./download_bookshelf_oa.sh [output_dir] [limit]
#     limit=0 → all; limit=N → first N packages
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

curl -sS -L --fail --connect-timeout 30 --max-time 180 \
  "$BASE/file_list.txt" -o "$ABS_OUT/file_list.txt"
curl -sS -L --fail --connect-timeout 30 --max-time 180 \
  "$BASE/file_list.csv" -o "$ABS_OUT/file_list.csv" || true

# Paths in column 1 (tab-separated). Avoid pipefail+head SIGPIPE on macOS.
PACKAGES_FILE="$ABS_OUT/packages_paths.txt"
awk -F'\t' '{
  gsub(/\r/, "", $1)
  if ($1 ~ /\.tar\.gz$/) print $1
}' "$ABS_OUT/file_list.txt" | sort -u > "$PACKAGES_FILE"

if [[ ! -s "$PACKAGES_FILE" && -f "$ABS_OUT/file_list.csv" ]]; then
  awk -F',' 'NR>1 {
    gsub(/"/, "", $1)
    gsub(/\r/, "", $1)
    if ($1 ~ /\.tar\.gz$/) print $1
  }' "$ABS_OUT/file_list.csv" | sort -u > "$PACKAGES_FILE"
fi

if [[ ! -s "$PACKAGES_FILE" ]]; then
  echo "ERROR: no .tar.gz paths found in file_list" >&2
  head -5 "$ABS_OUT/file_list.txt" >&2 || true
  exit 1
fi

total="$(wc -l < "$PACKAGES_FILE" | tr -d ' ')"
echo "→ Packages listed: $total"

if [[ "$LIMIT" =~ ^[1-9][0-9]*$ ]]; then
  # awk-based limit avoids SIGPIPE under pipefail
  awk -v n="$LIMIT" 'NR<=n' "$PACKAGES_FILE" > "$PACKAGES_FILE.limited"
  mv "$PACKAGES_FILE.limited" "$PACKAGES_FILE"
  echo "→ Limited to: $(wc -l < "$PACKAGES_FILE" | tr -d ' ')"
fi

sample="$(head -n1 "$PACKAGES_FILE")"
echo "→ Sample path: $sample"
echo "→ Sample URL : $BASE/$sample"

URL_LIST="$ABS_OUT/urls_to_download.txt"
: > "$URL_LIST"
need=0
skip=0

while IFS= read -r rel || [[ -n "$rel" ]]; do
  [[ -z "$rel" ]] && continue
  rel="${rel#./}"
  fname="$(basename "$rel")"
  parent="$(dirname "$rel")"
  mkdir -p "$ABS_OUT/packages/$parent"
  local_path="$ABS_OUT/packages/$rel"
  remote="$BASE/$rel"

  if [[ -f "$local_path" && -s "$local_path" ]]; then
    remote_len="$(curl -sS -I -L --connect-timeout 20 --max-time 60 "$remote" 2>/dev/null \
      | tr -d '\r' | awk 'tolower($1)=="content-length:"{print $2; exit}')"
    local_len="$(wc -c < "$local_path" | tr -d '[:space:]')"
    if [[ -n "${remote_len:-}" && "$remote_len" =~ ^[0-9]+$ && "$local_len" -eq "$remote_len" ]]; then
      echo "  [Skip] $rel"
      skip=$((skip + 1))
      continue
    fi
    echo "  [Update] $rel"
    rm -f "$local_path"
  else
    echo "  [New] $rel"
  fi
  {
    echo "$remote"
    echo "  dir=$ABS_OUT/packages/$parent"
    echo "  out=$fname"
  } >> "$URL_LIST"
  need=$((need + 1))
done < "$PACKAGES_FILE"

echo "  Summary: skip=$skip download=$need"
cp "$PACKAGES_FILE" "$ABS_OUT/remote_manifest.txt"

if [[ ! -s "$URL_LIST" ]]; then
  echo "→ Nothing new to download."
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "$ABS_OUT/last_sync_utc.txt"
  exit 0
fi

first_url="$(head -n1 "$URL_LIST")"
code="$(curl -sS -o /dev/null -w '%{http_code}' -L --connect-timeout 20 --max-time 60 "$first_url" || true)"
echo "→ Probe first package HTTP $code"
if [[ "$code" != "200" && "$code" != "206" ]]; then
  echo "ERROR: first package not fetchable (HTTP $code): $first_url" >&2
  exit 1
fi

aria2c -c -x "$CONNECTIONS" -s "$SPLITS" -j "$JOBS" \
  --max-tries="$MAX_TRIES" --retry-wait="$RETRY_WAIT" \
  --auto-file-renaming=false --allow-overwrite=true \
  --file-allocation=none \
  -i "$URL_LIST"

rm -f "$URL_LIST"
date -u +"%Y-%m-%dT%H:%M:%SZ" > "$ABS_OUT/last_sync_utc.txt"

echo "=================================================="
echo "Bookshelf OA sync complete → $ABS_OUT"
du -sh "$ABS_OUT/packages" 2>/dev/null || true
find "$ABS_OUT/packages" -name '*.tar.gz' | wc -l | awk '{print "tar.gz count:", $1}'
echo "=================================================="
