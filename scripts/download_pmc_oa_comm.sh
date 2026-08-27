#!/usr/bin/env bash
# ============================================================
# Episteme – PMC Commercial OA (oa_comm) Downloader
# Source : s3://pmc-oa-opendata (AWS Open Data, no sign-in)
# Restartable via: aws s3 sync
# ============================================================
set -euo pipefail

S3_BUCKET="s3://pmc-oa-opendata"
SUBSET="oa_comm"

OUTPUT_ROOT="${1:-./01_raw/pmc/oa_comm}"
FORMATS="${2:-xml}"          # space-separated: xml | txt | xml txt
DRY_RUN="${3:-false}"        # true = plan only

echo "=================================================="
echo "PMC Commercial OA (oa_comm) Downloader"
echo "Bucket subset : ${S3_BUCKET}/${SUBSET}/"
echo "Target root   : $OUTPUT_ROOT"
echo "Formats       : $FORMATS"
echo "Dry-run       : $DRY_RUN"
echo "=================================================="

if ! command -v aws >/dev/null 2>&1; then
  echo "ERROR: AWS CLI required." >&2
  echo "  macOS: brew install awscli" >&2
  exit 1
fi

mkdir -p "$OUTPUT_ROOT"
ABS_OUT="$(cd "$OUTPUT_ROOT" && pwd)"

# Optional: rough remote size (best-effort; can be slow on first call)
echo ""
echo "→ Estimating remote size (may take a minute)..."
for fmt in $FORMATS; do
  SRC="${S3_BUCKET}/${SUBSET}/${fmt}/"
  # --summarize works on recursive ls; can be heavy — comment out if too slow
  if summary="$(aws s3 ls "$SRC" --recursive --summarize --no-sign-request 2>/dev/null | tail -n 2)"; then
    echo "  [$fmt]"
    echo "$summary" | sed 's/^/    /'
  else
    echo "  [$fmt] size estimate unavailable (continuing)"
  fi
done

SYNC_FLAGS=(--no-sign-request --only-show-errors)
if [[ "$DRY_RUN" == "true" ]]; then
  SYNC_FLAGS+=(--dryrun)
  echo ""
  echo "DRY-RUN mode: no files will be written."
fi

for fmt in $FORMATS; do
  case "$fmt" in
    xml|txt) ;;
    *)
      echo "ERROR: unknown format '$fmt' (use xml and/or txt)" >&2
      exit 1
      ;;
  esac

  SRC="${S3_BUCKET}/${SUBSET}/${fmt}/"
  DEST="${ABS_OUT}/${fmt}"
  mkdir -p "$DEST"

  echo ""
  echo "→ Syncing $fmt"
  echo "  From: $SRC"
  echo "  To  : $DEST"

  # Main packages
  aws s3 sync "$SRC" "$DEST" "${SYNC_FLAGS[@]}"

  # Also pull any top-level filelists for this subset if present
  # (paths vary; best-effort — ignore failures)
  for list_pattern in \
      "${S3_BUCKET}/${SUBSET}/${fmt}/*.filelist.csv" \
      "${S3_BUCKET}/${SUBSET}/${fmt}/*.filelist.txt"
  do
    aws s3 sync "$SRC" "$DEST" \
      "${SYNC_FLAGS[@]}" \
      --exclude "*" \
      --include "*.filelist.csv" \
      --include "*.filelist.txt" \
      2>/dev/null || true
  done

  echo "  Finished $fmt"
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "$DEST/last_sync_utc.txt"
done

# Record what we intended
{
  echo "subset=oa_comm"
  echo "formats=$FORMATS"
  echo "source=${S3_BUCKET}/${SUBSET}/"
  echo "synced_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$ABS_OUT/sync_meta.txt"

echo ""
echo "=================================================="
echo "PMC oa_comm sync complete → $ABS_OUT"
echo "Re-run the same command anytime; only deltas transfer."
echo "=================================================="
echo "NOTE: oa_comm = commercial-use-allowed licenses only."
echo "Keep oa_noncomm / oa_other out of commercial training."
