#!/usr/bin/env bash

# ============================================================
# Episteme - PubMed Failed Files Repair Script
# ============================================================

set -euo pipefail

DATA_DIR="${1:-./01_raw/pubmed}"
BASELINE_DIR="$DATA_DIR/baseline"
UPDATES_DIR="$DATA_DIR/updates"
MD5_DIR="$DATA_DIR/md5"
FAILED_LIST="$DATA_DIR/failed_files.txt"

BASE_URL_BASELINE="ftp://ftp.ncbi.nlm.nih.gov/pubmed/baseline"
BASE_URL_UPDATES="ftp://ftp.ncbi.nlm.nih.gov/pubmed/updatefiles"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

mkdir -p "$MD5_DIR"
rm -f "$FAILED_LIST"

echo "=================================================="
echo "PubMed Failed Files Repair Tool"
echo "Data directory: $DATA_DIR"
echo "=================================================="

extract_md5() {
    local md5_file="$1"
    grep -oE '[a-fA-F0-9]{32}' "$md5_file" | head -n1
}

check_and_collect_failures() {
    local dir="$1"
    local label="$2"
    local base_url="$3"

    if [[ ! -d "$dir" ]]; then
        echo -e "${YELLOW}⚠  Skipping $label (directory not found)${NC}"
        return
    fi

    echo ""
    echo -e "${BOLD}${BLUE}→ Checking $label${NC}"

    local failed_in_section=0

    while IFS= read -r -d '' file; do
        filename=$(basename "$file")
        md5_file="$MD5_DIR/${filename}.md5"

        if [[ ! -f "$md5_file" ]]; then
            md5_file="${file}.md5"
        fi

        if [[ ! -f "$md5_file" ]]; then
            echo -e "  ${YELLOW}⚠  Missing MD5 (will re-download): $filename${NC}"
            echo "$base_url/$filename" >> "$FAILED_LIST"
            failed_in_section=$((failed_in_section + 1))
            continue
        fi

        expected=$(extract_md5 "$md5_file")
        actual=$(md5sum "$file" | awk '{print $1}')

        if [[ -z "$expected" || "$expected" != "$actual" ]]; then
            echo -e "  ${RED}✗ Mismatch: $filename${NC}"
            echo "$base_url/$filename" >> "$FAILED_LIST"
            failed_in_section=$((failed_in_section + 1))
        fi
    done < <(find "$dir" -maxdepth 1 -name "pubmed26n*.xml.gz" -print0 | sort -z)

    echo "  Failed / Missing in $label: $failed_in_section"
}

# -------- Step 1: Find failed files --------
check_and_collect_failures "$BASELINE_DIR" "Baseline" "$BASE_URL_BASELINE"
check_and_collect_failures "$UPDATES_DIR"  "Updates"  "$BASE_URL_UPDATES"

if [[ ! -s "$FAILED_LIST" ]]; then
    echo ""
    echo -e "${GREEN}No failed or missing files found. Everything looks good.${NC}"
    exit 0
fi

FAILED_COUNT=$(wc -l < "$FAILED_LIST" | tr -d ' ')
echo ""
echo -e "${BOLD}Total files to repair: $FAILED_COUNT${NC}"
echo "List saved to: $FAILED_LIST"

# -------- Step 2: Delete bad local files --------
echo ""
echo "→ Deleting corrupted/missing local files..."

while read -r url; do
    filename=$(basename "$url")

    rm -f "$BASELINE_DIR/$filename"
    rm -f "$UPDATES_DIR/$filename"
    rm -f "$MD5_DIR/${filename}.md5"
    rm -f "$BASELINE_DIR/${filename}.md5"
    rm -f "$UPDATES_DIR/${filename}.md5"

    echo "  Removed: $filename"
done < "$FAILED_LIST"

# -------- Step 3: Re-download failed XML files --------
echo ""
echo "→ Re-downloading failed XML files..."

aria2c -c \
  -x 8 -s 8 -j 5 \
  --max-tries=15 \
  --retry-wait=20 \
  --auto-file-renaming=false \
  --allow-overwrite=true \
  -i "$FAILED_LIST"

# -------- Step 4: Re-download corresponding MD5 files --------
echo ""
echo "→ Re-downloading corresponding MD5 files..."

sed 's|$|.md5|' "$FAILED_LIST" > "${FAILED_LIST}.md5urls"

aria2c -c \
  -x 6 -s 6 -j 8 \
  --max-tries=10 \
  --retry-wait=15 \
  --auto-file-renaming=false \
  --allow-overwrite=true \
  --dir="$MD5_DIR" \
  -i "${FAILED_LIST}.md5urls"

echo ""
echo "=================================================="
echo -e "${GREEN}Repair download completed.${NC}"
echo "Please run the verification script again:"
echo "  ./verify_pubmed_checksums.sh $DATA_DIR"
echo "=================================================="
