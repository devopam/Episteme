#!/usr/bin/env bash
# ============================================================
# Episteme – Europe PMC Preprints & Preprint-Abstracts
# Portable: macOS Bash 3.2+ / Linux
# ============================================================
set -euo pipefail

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

log()  { printf '%s\n' "$*"; }
err()  { printf 'ERROR: %s\n' "$*" >&2; }
warn() { printf 'WARN: %s\n' "$*" >&2; }

get_local_size() {
  local f="$1"
  if [[ -f "$f" ]]; then
    wc -c < "$f" | tr -d '[:space:]'
  else
    echo 0
  fi
}

ftp_list_names() {
  local url="$1"
  local pattern="$2"
  local tmp
  tmp="$(mktemp)"

  if curl -sS --list-only --connect-timeout 30 --max-time 120 "$url" 2>/dev/null \
      | grep -E "$pattern" > "$tmp"; then
    :
  else
    curl -sS --connect-timeout 30 --max-time 120 "$url" 2>/dev/null \
      | awk '{print $NF}' \
      | grep -E "$pattern" > "$tmp" || true
  fi

  sort -u "$tmp"
  rm -f "$tmp"
}

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
  size="$(echo "$line" | awk '{print $5}')"
  if [[ "$size" =~ ^[0-9]+$ ]]; then
    echo "$size"
  else
    echo 0
  fi
}

# Returns 0 if remote object is fetchable (1-byte range probe)
remote_fetchable() {
  local url="$1"
  curl -sS --fail -r 0-0 -o /dev/null --connect-timeout 15 --max-time 40 "$url" 2>/dev/null
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
      --file-allocation=none \
      -i "$url_list"
  ) || warn "aria2c reported errors (individual files may have failed; others may be OK)"
}

queue_files() {
  # Args: target_dir parent_url files_newline_list
  local target_dir="$1"
  local parent_url="$2"
  local files="$3"
  local url_list="$target_dir/urls_to_download.txt"
  local current_yyyymm
  current_yyyymm="$(date +%Y%m)"

  rm -f "$url_list"
  local skip=0 need=0 defer=0

  while IFS= read -r file_name; do
    [[ -z "$file_name" ]] && continue

    # Defer current-month abstract zips (often listed but not published)
    if [[ "$file_name" == *"_${current_yyyymm}.zip" ]]; then
      log "  [Defer] $file_name (current month – often unpublished)"
      defer=$((defer + 1))
      continue
    fi

    local local_file="$target_dir/$file_name"
    local remote_url="${parent_url}${file_name}"
    local local_size remote_size

    local_size="$(get_local_size "$local_file")"
    remote_size="$(get_remote_size "$parent_url" "$file_name")"

    if [[ "$local_size" -gt 0 && "$remote_size" -gt 0 && "$local_size" -eq "$remote_size" ]]; then
      log "  [Skip] $file_name (size match: $local_size)"
      skip=$((skip + 1))
      continue
    fi

    if [[ "$local_size" -gt 0 && "$remote_size" -eq 0 ]]; then
      log "  [Skip] $file_name (local present, remote size unknown)"
      skip=$((skip + 1))
      continue
    fi

    if ! remote_fetchable "$remote_url"; then
      log "  [Skip] $file_name (listed but not fetchable)"
      defer=$((defer + 1))
      continue
    fi

    if [[ "$local_size" -gt 0 ]]; then
      log "  [Update] $file_name (local=$local_size remote=$remote_size)"
      rm -f "$local_file"
    else
      log "  [New] $file_name"
    fi
    echo "$remote_url" >> "$url_list"
    need=$((need + 1))
  done <<< "$files"

  log "  Summary: skip=$skip  download=$need  deferred/unfetchable=$defer"
  download_url_list "$target_dir" "$url_list"
  rm -f "$url_list"
}

download_preprints() {
  local target_dir="$ABS_OUTPUT_ROOT/preprints"
  mkdir -p "$target_dir"

  log ""
  log "→ Discovering preprint files at ${FTP_HOST}${PREPRINTS_PATH}"

  local files
  files="$(ftp_list_names "${FTP_HOST}${PREPRINTS_PATH}" '\.xml\.gz$')"

  if [[ -z "$files" ]]; then
    err "No preprint .xml.gz files discovered."
    err "Try: curl --list-only ${FTP_HOST}${PREPRINTS_PATH}"
    return 1
  fi

  local remote_count
  remote_count="$(printf '%s\n' "$files" | grep -c . || true)"
  log "  Remote files discovered: $remote_count"
  printf '%s\n' "$files" > "$target_dir/remote_manifest.txt"

  queue_files "$target_dir" "${FTP_HOST}${PREPRINTS_PATH}" "$files"
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "$target_dir/last_sync_utc.txt"
}

download_abstracts() {
  local target_dir="$ABS_OUTPUT_ROOT/preprint_abstracts"
  mkdir -p "$target_dir"

  log ""
  log "→ Discovering preprint-abstract files at ${FTP_HOST}${ABSTRACTS_PATH}"

  local files
  files="$(ftp_list_names "${FTP_HOST}${ABSTRACTS_PATH}" '\.zip$')"

  if [[ -z "$files" ]]; then
    warn "No preprint-abstract .zip files discovered (upstream may be empty)."
    warn "Try: curl --list-only ${FTP_HOST}${ABSTRACTS_PATH}"
    return 0
  fi

  local remote_count
  remote_count="$(printf '%s\n' "$files" | grep -c . || true)"
  log "  Remote files discovered: $remote_count"
  printf '%s\n' "$files" > "$target_dir/remote_manifest.txt"

  queue_files "$target_dir" "${FTP_HOST}${ABSTRACTS_PATH}" "$files"
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "$target_dir/last_sync_utc.txt"
}

# ---- main ----
mkdir -p "$OUTPUT_ROOT"
ABS_OUTPUT_ROOT="$(cd "$OUTPUT_ROOT" && pwd)"

log "=================================================="
log "Europe PMC Preprints / Abstracts Downloader"
log "Target root : $ABS_OUTPUT_ROOT"
log "Subset      : $SUBSET"
log "=================================================="

command -v aria2c >/dev/null 2>&1 || { err "aria2c required"; exit 1; }
command -v curl   >/dev/null 2>&1 || { err "curl required"; exit 1; }

case "$SUBSET" in
  preprints) download_preprints ;;
  abstracts) download_abstracts ;;
  both)
    download_preprints || warn "preprints step reported issues"
    download_abstracts || warn "abstracts step reported issues"
    ;;
  *)
    err "Invalid subset '$SUBSET'. Use: preprints | abstracts | both"
    exit 1
    ;;
esac

log ""
log "=================================================="
log "Europe PMC preprints/abstracts sync finished."
log "=================================================="
