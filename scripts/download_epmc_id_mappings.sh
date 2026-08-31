#!/usr/bin/env bash
# ============================================================
# Episteme – Europe PMC PMID–PMCID–DOI mappings
# Portable: macOS Bash 3.2+ / Linux  (no mapfile)
# ============================================================
set -euo pipefail

BASE_URL="https://europepmc.org/ftp/DOI_mappings"
OUTPUT_DIR="${1:-./01_raw/europepmc/id_mappings}"

CONNECTIONS=4
SPLITS=4
MAX_TRIES=10
RETRY_WAIT=20

echo "=================================================="
echo "Europe PMC ID Mappings Downloader"
echo "Source : $BASE_URL"
echo "Target : $OUTPUT_DIR"
echo "=================================================="

mkdir -p "$OUTPUT_DIR"
ABS_OUT="$(cd "$OUTPUT_DIR" && pwd)"

command -v aria2c >/dev/null 2>&1 || { echo "ERROR: aria2c required" >&2; exit 1; }
command -v curl   >/dev/null 2>&1 || { echo "ERROR: curl required" >&2; exit 1; }

TMP_LIST="$(mktemp)"
curl -sS --fail --connect-timeout 30 --max-time 120 "$BASE_URL/" -o "$TMP_LIST" || {
  echo "ERROR: could not list $BASE_URL" >&2
  rm -f "$TMP_LIST"
  exit 1
}

# Portable discovery (no mapfile)
FILES="$(
  grep -oE 'href="[^"]+"' "$TMP_LIST" \
    | sed -E 's/href="//; s/"$//' \
    | grep -vE '^\?|^/|^\.\./|^#' \
    | grep -E '\.(csv|tsv|txt|gz|zip)(\?|$)' \
    | sed 's/[?].*$//' \
    | sort -u
)"
rm -f "$TMP_LIST"

if [[ -z "$FILES" ]]; then
  echo "→ HTML parse found no files; trying common filenames..."
  for candidate in \
    "PMID_PMCID_DOI.csv.gz" \
    "PMID_PMCID_DOI.txt.gz" \
    "PMC-ids.csv.gz" \
    "ids.csv.gz"
  do
    code="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 15 --max-time 30 \
      "$BASE_URL/$candidate" || true)"
    if [[ "$code" == "200" ]]; then
      FILES="${FILES:+$FILES$'\n'}$candidate"
    fi
  done
fi

# Drop privacy notices from download queue (optional keep on disk if already present)
FILES="$(printf '%s\n' "$FILES" | grep -viE 'privacy|readme' || true)"

if [[ -z "$FILES" ]]; then
  echo "ERROR: No mapping files discovered under $BASE_URL" >&2
  exit 1
fi

file_count="$(printf '%s\n' "$FILES" | grep -c . || true)"
echo "→ Discovered $file_count file(s):"
printf '%s\n' "$FILES" | sed 's/^/   /'
printf '%s\n' "$FILES" > "$ABS_OUT/remote_manifest.txt"

URL_LIST="$ABS_OUT/urls_to_download.txt"
rm -f "$URL_LIST"
need=0
skip=0

while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  local_path="$ABS_OUT/$f"
  remote_url="$BASE_URL/$f"

  if [[ -f "$local_path" && -s "$local_path" ]]; then
    remote_len="$(curl -sS -I --connect-timeout 15 --max-time 30 "$remote_url" 2>/dev/null \
      | tr -d '\r' | awk 'tolower($1)=="content-length:"{print $2; exit}')"
    local_len="$(wc -c < "$local_path" | tr -d '[:space:]')"
    if [[ -n "$remote_len" && "$remote_len" =~ ^[0-9]+$ && "$local_len" -eq "$remote_len" ]]; then
      echo "  [Skip] $f (size match: $local_len)"
      skip=$((skip + 1))
      continue
    fi
    if [[ -n "$remote_len" && "$remote_len" =~ ^[0-9]+$ && "$local_len" -ne "$remote_len" ]]; then
      echo "  [Update] $f (local=$local_len remote=$remote_len)"
      rm -f "$local_path"
    else
      echo "  [Skip] $f (local present; remote size unknown)"
      skip=$((skip + 1))
      continue
    fi
  else
    echo "  [New] $f"
  fi
  echo "$remote_url" >> "$URL_LIST"
  need=$((need + 1))
done <<< "$FILES"

echo "  Summary: skip=$skip  download=$need"

if [[ -s "$URL_LIST" ]]; then
  (
    cd "$ABS_OUT"
    aria2c -c \
      -x "$CONNECTIONS" \
      -s "$SPLITS" \
      -j 2 \
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
echo "ID mappings sync complete → $ABS_OUT"
echo "=================================================="
