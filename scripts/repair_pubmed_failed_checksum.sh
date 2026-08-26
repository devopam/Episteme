#!/usr/bin/env bash

# ============================================================
# Episteme - PubMed Failed Files Repair Script
# 1. Verifies checksums
# 2. Collects failed files
# 3. Deletes the bad .xml.gz files
# 4. Re-downloads only the failed ones (XML + MD5)
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
