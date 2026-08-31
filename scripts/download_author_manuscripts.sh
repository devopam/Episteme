#!/usr/bin/env bash
# ============================================================
# Episteme – Author Manuscript Collection (Europe PMC)
# Baseline + daily incrementals, XML (default), restartable
# Portable: macOS Bash 3.2+ / Linux
# ============================================================
set -euo pipefail

BASE_URL="https://europepmc.org/ftp/manuscripts"
OUTPUT_DIR="${1:-./01_raw/europepmc/author_manuscripts}"
FORMAT="${2:-xml}"          # xml | txt
MODE="${3:-all}"            # all | baseline | incr

CONNECTIONS=8
SPLITS=8
MAX_JOBS=3
MAX_TRIES=12
RETRY_WAIT=30

echo "=================================================="
echo "Author Manuscript Downloader (Europe PMC)"
echo "Source : $BASE_URL"
echo "Target : $OUTPUT_DIR"
echo "Format : $FORMAT"
echo "Mode   : $MODE"
echo "=================================================="

if [[ "$FORMAT" != "xml" && "$FORMAT" != "txt" ]]; then
  echo "ERROR: FORMAT must be xml or txt" >&2
  exit 1
fi
if [[ "$MODE" != "all" && "$MODE" != "baseline" && "$MODE" != "incr" ]]; then
  echo "ERROR: MODE must be all | baseline | incr" >&2
  exit 1
fi

command -v aria2c >/dev/null 2>&1 || { echo "ERROR: aria2c required" >&2; exit 1; }
command -v curl   >/dev/null 2>&1 || { echo "ERROR: curl required" >&2; exit 1; }

mkdir -p "$OUTPUT_DIR/$FORMAT"
ABS_OUT="$(cd "$OUTPUT_DIR" && pwd)"
TARGET="$ABS_OUT/$FORMAT"
mkdir -p "$TARGET"

TMP_HTML="$(mktemp)"
if ! curl -sS --fail --connect-timeout 30 --max-time 180 "$BASE_URL/" -o "$TMP_HTML"; then
  echo "ERROR: could not list $BASE_URL/" >&2
  rm -f "$TMP_HTML"
  exit 1
fi

ALL_NAMES="$(
  grep -oE 'href="[^"]+"' "$TMP_HTML" \
    | sed -E 's/href="//; s/"$//' \
    | grep -vE '^\?|^/|^\.\./|^#' \
    | sed 's/[?].*$//' \
    | sort -u
)"
rm -f "$TMP_HTML"

PREFIX="author_manuscript_${FORMAT}"
CANDIDATES=""

while IFS= read -r name; do
  [[ -z "$name" ]] && continue
  case "$name" in
    ${PREFIX}.*) ;;
    *) continue ;;
  esac
  case "$MODE" in
    baseline) [[ "$name" == *".baseline."* ]] || continue ;;
    incr)     [[ "$name" == *".incr."* ]] || continue ;;
    all) ;;
  esac
  case "$name" in
    *.tar.gz|*.filelist.csv|*.filelist.txt) ;;
    *) continue ;;
  esac
  CANDIDATES="${CANDIDATES:+$CANDIDATES$'\n'}$name"
done <<< "$ALL_NAMES"

if [[ -z "$CANDIDATES" ]]; then
  echo "ERROR: No matching files for format=$FORMAT mode=$MODE" >&2
  echo "Inspect $BASE_URL/ in a browser; naming may have changed." >&2
  exit 1
fi

COUNT="$(printf '%s\n' "$CANDIDATES" | grep -c . || true)"
echo "→ Discovered $COUNT remote file(s)"
printf '%s\n' "$CANDIDATES" > "$TARGET/remote_manifest.txt"
printf '%s\n' "$CANDIDATES" | sed 's/^/   /' | head -n 30
if [[ "$COUNT" -gt 30 ]]; then
  echo "   ... ($((COUNT - 30)) more)"
fi

URL_LIST="$TARGET/urls_to_download.txt"
rm -f "$URL_LIST"
need=0
skip=0

while IFS= read -r name; do
  [[ -z "$name" ]] && continue
  local_path="$TARGET/$name"
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
    cd "$TARGET"
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
  ) || echo "WARN: aria2c reported some errors; check incomplete files" >&2
  rm -f "$URL_LIST"
else
  echo "→ Nothing new to download."
fi

date -u +"%Y-%m-%dT%H:%M:%SZ" > "$TARGET/last_sync_utc.txt"

echo ""
echo "=================================================="
echo "Author manuscripts sync complete → $TARGET"
echo "Manifest: $TARGET/remote_manifest.txt"
echo "=================================================="
echo "NOTE: License = text mining / applicable copyright."
echo "Do not mix into commercial training corpus without review."
