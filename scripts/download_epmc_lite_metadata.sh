#!/usr/bin/env bash
# ============================================================
# Episteme – Europe PMC full-text lite metadata (restartable)
# Source: https://europepmc.org/ftp/pmclitemetadata/
# Portable: macOS + Linux (Bash 3.2+)
# ============================================================
set -euo pipefail

BASE_URL="https://europepmc.org/ftp/pmclitemetadata"
OUTPUT_DIR="${1:-./01_raw/europepmc/metadata_lite}"

CONNECTIONS=6
SPLITS=6
MAX_JOBS=3
MAX_TRIES=12
RETRY_WAIT=20

echo "=================================================="
echo "Europe PMC Lite Metadata Downloader"
echo "Source : $BASE_URL"
echo "Target : $OUTPUT_DIR"
echo "=================================================="

for cmd in curl aria2c; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: $cmd required" >&2
    exit 1
  fi
done

mkdir -p "$OUTPUT_DIR"
ABS_OUT="$(cd "$OUTPUT_DIR" && pwd)"

TMP_HTML="$(mktemp)"
if ! curl -sS --fail --connect-timeout 30 --max-time 180 "$BASE_URL/" -o "$TMP_HTML"; then
  echo "ERROR: could not list $BASE_URL/" >&2
  rm -f "$TMP_HTML"
  exit 1
fi

# Discover likely data files from HTML index
CANDIDATES="$(
  grep -oE 'href="[^"]+"' "$TMP_HTML" \
    | sed -E 's/href="//; s/"$//' \
    | grep -vE '^\?|^/|^\.\./|^#' \
    | sed 's/[?].*$//' \
    | grep -E '\.(xml|xml\.gz|tar\.gz|zip|csv|txt)(\?|$)|metadata|pmc' \
    | grep -viE 'privacy|readme|\.html?$' \
    | sort -u
)"
rm -f "$TMP_HTML"

# If HTML filter was too strict, fall back to any non-directory-looking hrefs
if [[ -z "$CANDIDATES" ]]; then
  TMP_HTML="$(mktemp)"
  curl -sS --fail --connect-timeout 30 --max-time 180 "$BASE_URL/" -o "$TMP_HTML"
  CANDIDATES="$(
    grep -oE 'href="[^"]+"' "$TMP_HTML" \
      | sed -E 's/href="//; s/"$//' \
      | grep -vE '^\?|^/|^\.\./|^#|/$' \
      | sed 's/[?].*$//' \
      | grep -viE 'privacy|readme' \
      | sort -u
  )"
  rm -f "$TMP_HTML"
fi

if [[ -z "$CANDIDATES" ]]; then
  echo "ERROR: No files discovered under $BASE_URL" >&2
  echo "Open the URL in a browser and adjust filters if naming changed." >&2
  exit 1
fi

COUNT="$(echo "$CANDIDATES" | grep -c . || true)"
echo "→ Discovered $COUNT remote file(s)"
echo "$CANDIDATES" > "$ABS_OUT/remote_manifest.txt"
echo "$CANDIDATES" | sed 's/^/   /'

URL_LIST="$ABS_OUT/urls_to_download.txt"
rm -f "$URL_LIST"
need=0
skip=0

while IFS= read -r name; do
  [[ -z "$name" ]] && continue
  local_path="$ABS_OUT/$name"
  remote_url="$BASE_URL/$name"

  if [[ -f "$local_path" && -s "$local_path" ]]; then
    remote_len="$(curl -sS -I --connect-timeout 20 --max-time 60 "$remote_url" 2>/dev/null \
      | tr -d '\r' | awk 'tolower($1)=="content-length:"{print $2; exit}')"
    local_len="$(wc -c < "$local_path" | tr -d '[:space:]')"
    if [[ -n "$remote_len" && "$remote_len" =~ ^[0-9]+$ && "$local_len" -eq "$remote_len" ]]; then
      echo "  [Skip] $name"
      skip=$((skip + 1))
      continue
    fi
    if [[ -n "$remote_len" && "$remote_len" =~ ^[0-9]+$ && "$local_len" -ne "$remote_len" ]]; then
      echo "  [Update] $name (local=$local_len remote=$remote_len)"
      rm -f "$local_path"
    else
      echo "  [Skip] $name (local present; remote size unknown)"
      skip=$((skip + 1))
      continue
    fi
  else
    echo "  [New] $name"
  fi
  echo "$remote_url" >> "$URL_LIST"
  need=$((need + 1))
done <<< "$CANDIDATES"

echo "  Summary: skip=$skip  download=$need"

if [[ -s "$URL_LIST" ]]; then
  (
    cd "$ABS_OUT"
    aria2c -c \
      -x "$CONNECTIONS" \
      -s "$SPLITS" \
      -j "$MAX_JOBS" \
      --max-tries="$MAX_TRIES" \
      --retry-wait="$RETRY_WAIT" \
      --auto-file-renaming=false \
      --allow-overwrite=true \
      --file-allocation=none \
      -i "$URL_LIST"
  )
  rm -f "$URL_LIST"
else
  echo "→ Nothing new to download."
fi

date -u +"%Y-%m-%dT%H:%M:%SZ" > "$ABS_OUT/last_sync_utc.txt"

echo ""
echo "=================================================="
echo "Lite metadata sync complete → $ABS_OUT"
echo "=================================================="
