#!/usr/bin/env bash

# ============================================================
# Episteme - PMC Commercial OA (oa_comm) Downloader
# Primary source: AWS Open Data (s3://pmc-oa-opendata)
# ============================================================

set -euo pipefail

# Configuration
S3_BUCKET="s3://pmc-oa-opendata"
SUBSET="oa_comm"
FORMATS=("xml")                          # Add "txt" later if desired
OUTPUT_ROOT="${1:-./01_raw/pmc/oa_comm}"

echo "=================================================="
echo "PMC Commercial OA Downloader"
echo "Target root: $OUTPUT_ROOT"
echo "=================================================="

for FORMAT in "${FORMATS[@]}"; do
  SRC="${S3_BUCKET}/${SUBSET}/${FORMAT}/"
  DEST="${OUTPUT_ROOT}/${FORMAT}"

  echo ""
  echo "→ Syncing $FORMAT ..."
  echo "  From: $SRC"
  echo "  To  : $DEST"

  mkdir -p "$DEST"

  aws s3 sync \
    "$SRC" \
    "$DEST" \
    --no-sign-request \
    --only-show-errors

  echo "  Finished $FORMAT"
done

echo ""
echo "=================================================="
echo "Download / sync completed."
echo "Data location: $OUTPUT_ROOT"
echo "=================================================="
