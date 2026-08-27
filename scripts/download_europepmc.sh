#!/usr/bin/env bash
# ============================================================
# Episteme - Europe PMC Preprints & Preprint-Abstracts Downloader
# Hardened: robust listing, size checks, discovery logging
# Portable: Linux & macOS
# ============================================================

set -euo pipefail

# -------------------- Configuration --------------------
FTP_HOST="ftp://ftp.ebi.ac.uk"
PREPRINTS_PATH="/pub/databases/pmc/preprints/"
ABSTRACTS_PATH="/pub/databases/pmc/preprint_abstracts/"

OUTPUT_ROOT="${1:-./01_raw/europepmc}"
SUBSET="${2:-both}"   # preprints | abstracts | both

CONNECTIONS_PER_SERVER=8
SPLITS=8
MAX_CONCURRENT_DOWNLOADS=4
MAX_TRIES=12
RETRY_WAIT=20

# -------------------- Helpers --------------------
log()  { printf '%s\n' "$*"; }
err()  { printf 'ERROR: %s\n' "$*" >&2; }

get_local_size() {
  local f="$1"
  if [[ -f "$f" ]]; then
    wc -c < "$f" | tr -d '[:space:]'
  else
    echo 0
  fi
}

# Robust FTP name listing.
# Tries curl --list-only first; falls back to parsing a full LIST.
ftp_list_names() {
  local url="$1"
  local pattern="$2"   # e.g. '\.xml\.gz$' or '\.zip$'
  local tmp
  tmp="$(mktemp)"

  # Attempt 1: NLST-style list-only
  if curl -sS --list-only --connect-timeout 30 --max-time 120 "$url" 2>/dev/null \
      | grep -E "$pattern" > "$tmp"; then
    :
  else
    # Attempt 2: full LIST, take last column as name
    curl -sS --connect-timeout 30 --max-time 120 "$url" 2>/dev/null \
      | awk '{print $NF}' \
      | grep -E "$pattern" > "$tmp" || true
  fi

  # Deduplicate + sort
  sort -u "$tmp"
  rm -f "$tmp"
}

# Best-effort remote size from a directory listing line.
# Returns 0 if unknown.
get_remote_size() {
  local parent_url="$1"
  local file_name="$2"
  local line size

  line="$(curl -sS --connect-timeout 20 --max-time 60 "$parent_url" 2>/dev/null \
            | grep -F "$file_name" | head -n1 || true)"

  if [[ -z "$line" ]]; then
    echo 0
    return
  fi

  # Typical LIST: permissions links user group size month day time/year name
  size="$(echo "$line" | awk '{print $5}')"
  if [[ "$size" =~ ^[0-9]+$ ]]; then
    echo "$size"
  else
    echo 0
  fi
}

download_url_list() {
  local target_dir="$1"
  local url_list="$2"

  if [[ ! -s "$url_list" ]]; then
    log "  Nothing new to download."
    return 0
  fi

  local count
  count="$(wc -l < "$url_list" | tr -d '[:space:]')"
  log "  Downloading $count file(s) with aria2c..."

  (
    cd "$target_dir"
    aria2c -c \
      -x "$CONNECTIONS_PER_SERVER" \
      -s "$SPLITS" \
      -j "$MAX_CONCURRENT_DOWNLOADS" \
      --max-tries="$MAX_TRIES" \
      --retry-wait="$RETRY_WAIT" \
      --auto-file-renaming=false \
      --allow-overwrite=true \
      -i "$url_list"
  )
}

# -------------------- Preprints --------------------
download_preprints() {
  local target_dir="$ABS_OUTPUT_ROOT/preprints"
  mkdir -p "$target_dir"

  log ""
  log "→ Discovering preprint files at ${FTP_HOST}${PREPRINTS_PATH}"

  local files
  files="$(ftp_list_names "${FTP_HOST}${PREPRINTS_PATH}" '\.xml\.gz$')"

  if [[ -z "$files" ]]; then
    err "No preprint .xml.gz files discovered. Listing may have failed."
    err "Try manually: curl --list-only ${FTP_HOST}${PREPRINTS_PATH}"
    exit 1
  fi

  local remote_count
  remote_count="$(printf '%s\n' "$files" | grep -c . || true)"
  log "  Remote files discovered: $remote_count"

  # Persist discovery manifest for audit
  printf '%s\n' "$files" > "$target_dir/remote_manifest.txt"

  local url_list="$target_dir/urls_to_download.txt"
  rm -f "$url_list"

  local skip=0 need=0

  while IFS= read -r file_name; do
    [[ -z "$file_name" ]] && continue

    local local_file="$target_dir/$file_name"
    local remote_url="${FTP_HOST}${PREPRINTS_PATH}${file_name}"
    local local_size remote_size

    local_size="$(get_local_size "$local_file")"
    remote_size="$(get_remote_size "${FTP_HOST}${PREPRINTS_PATH}" "$file_name")"

    if [[ "$local_size" -gt 0 && "$remote_size" -gt 0 && "$local_size" -eq "$remote_size" ]]; then
      log "  [Skip] $file_name (size match: $local_size)"
      skip=$((skip + 1))
    elif [[ "$local_size" -gt 0 && "$remote_size" -eq 0 ]]; then
      # Remote size unknown but local exists → skip to avoid needless re-download
      log "  [Skip] $file_name (local present, remote size unknown)"
      skip=$((skip + 1))
    else
      if [[ "$local_size" -gt 0 ]]; then
        log "  [Update] $file_name (local=$local_size remote=$remote_size)"
        rm -f "$local_file"
      else
        log "  [New] $file_name"
      fi
      echo "$remote_url" >> "$url_list"
      need=$((need + 1))
    fi
  done <<< "$files"

  log "  Summary: skip=$skip  download=$need"
  download_url_list "$target_dir" "$url_list"
  rm -f "$url_list"
}

# -------------------- Abstracts --------------------
download_abstracts() {
  local target_dir="$ABS_OUTPUT_ROOT/preprint_abstracts"
  mkdir -p "$target_dir"

  log ""
  log "→ Discovering preprint-abstract files at ${FTP_HOST}${ABSTRACTS_PATH}"

  local files
  files="$(ftp_list_names "${FTP_HOST}${ABSTRACTS_PATH}" '\.zip$')"

  if [[ -z "$files" ]]; then
    err "No preprint-abstract .zip files discovered. Listing may have failed."
    err "Try manually: curl --list-only ${FTP_HOST}${ABSTRACTS_PATH}"
    exit 1
  fi

  local remote_count
  remote_count="$(printf '%s\n' "$files" | grep -c . || true)"
  log "  Remote files discovered: $remote_count"
  printf '%s\n' "$files" > "$target_dir/remote_manifest.txt"

  local url_list="$target_dir/urls_to_download.txt"
  rm -f "$url_list"

  local skip=0 need=0

  while IFS= read -r file_name; do
    [[ -z "$file_name" ]] && continue

    local local_file="$target_dir/$file_name"
    local remote_url="${FTP_HOST}${ABSTRACTS_PATH}${file_name}"
    local local_size remote_size

    local_size="$(get_local_size "$local_file")"
    remote_size="$(get_remote_size "${FTP_HOST}${ABSTRACTS_PATH}" "$file_name")"

    if [[ "$local_size" -gt 0 && "$remote_size" -gt 0 && "$local_size" -eq "$remote_size" ]]; then
      log "  [Skip] $file_name (size match: $local_size)"
      skip=$((skip + 1))
    elif [[ "$local_size" -gt 0 && "$remote_size" -eq 0 ]]; then
      log "  [Skip] $file_name (local present, remote size unknown)"
      skip=$((skip + 1))
    else
      if [[ "$local_size" -gt 0 ]]; then
        log "  [Update] $file_name (local=$local_size remote=$remote_size)"
        rm -f "$local_file"
      else
        log "  [New] $file_name"
      fi
      echo "$remote_url" >> "$url_list"
      need=$((need + 1))
    fi
  done <<< "$files"

  log "  Summary: skip=$skip  download=$need"
  download_url_list "$target_dir" "$url_list"
  rm -f "$url_list"
}

# -------------------- Main --------------------
mkdir -p "$OUTPUT_ROOT"
ABS_OUTPUT_ROOT="$(cd "$OUTPUT_ROOT" && pwd)"

log "=================================================="
log "Europe PMC Incremental Downloader (hardened)"
log "Target root : $ABS_OUTPUT_ROOT"
log "Subset      : $SUBSET"
log "=================================================="

if ! command -v aria2c >/dev/null 2>&1; then
  err "aria2c is required but not found in PATH"
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  err "curl is required but not found in PATH"
  exit 1
fi

case "$SUBSET" in
  preprints) download_preprints ;;
  abstracts) download_abstracts ;;
  both)
    download_preprints
    download_abstracts
    ;;
  *)
    err "Invalid subset '$SUBSET'. Use: preprints | abstracts | both"
    exit 1
    ;;
esac

log ""
log "=================================================="
log "Europe PMC sync completed."
log "Manifests written under each subset dir as remote_manifest.txt"
log "=================================================="
