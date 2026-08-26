#!/usr/bin/env bash

# ============================================================
# Episteme - PubMed Checksum Verification Script
# Verifies .xml.gz files against their .md5 checksums
# Clear separation of Baseline vs Updates results
# ============================================================

set -euo pipefail

# Default paths (can be overridden by arguments)
DATA_DIR="${1:-./01_raw/pubmed}"
BASELINE_DIR="$DATA_DIR/baseline"
UPDATES_DIR="$DATA_DIR/updates"
MD5_DIR="$DATA_DIR/md5"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

echo "=================================================="
echo "PubMed Checksum Verification"
echo "Data directory: $DATA_DIR"
echo "=================================================="

# Global counters
TOTAL_ALL=0
PASSED_ALL=0
FAILED_ALL=0
MISSING_ALL=0

verify_directory() {
    local dir="$1"
    local label="$2"

    if [[ ! -d "$dir" ]]; then
        echo -e "${YELLOW}⚠  Directory not found: $dir — skipping $label${NC}"
        return
    fi

    echo ""
    echo -e "${BOLD}${BLUE}→ Verifying $label${NC}"
    echo "  Location: $dir"
    echo "--------------------------------------------------"

    local total=0
    local passed=0
    local failed=0
    local missing_md5=0

    while IFS= read -r -d '' file; do
        ((total++)) || true
        filename=$(basename "$file")
        md5_file="$MD5_DIR/${filename}.md5"

        # Fallback: md5 file sitting next to the data file
        if [[ ! -f "$md5_file" ]]; then
            md5_file="${file}.md5"
        fi

        if [[ ! -f "$md5_file" ]]; then
            echo -e "  ${YELLOW}⚠  Missing MD5: $filename${NC}"
            ((missing_md5++)) || true
            continue
        fi

        expected=$(awk '{print $1}' "$md5_file")
        actual=$(md5sum "$file" | awk '{print $1}')

        if [[ "$expected" == "$actual" ]]; then
            echo -e "  ${GREEN}✓${NC} $filename"
            ((passed++)) || true
        else
            echo -e "  ${RED}✗ FAILED: $filename${NC}"
            echo -e "      Expected: $expected"
            echo -e "      Actual:   $actual"
            ((failed++)) || true
        fi
    done < <(find "$dir" -maxdepth 1 -name "pubmed26n*.xml.gz" -print0 | sort -z)

    # Section summary
    echo ""
    echo -e "  ${BOLD}$label Summary:${NC}"
    echo "    Total files : $total"
    echo -e "    Passed      : ${GREEN}$passed${NC}"
    echo -e "    Failed      : ${RED}$failed${NC}"
    echo -e "    Missing MD5 : ${YELLOW}$missing_md5${NC}"

    # Update global counters
    TOTAL_ALL=$((TOTAL_ALL + total))
    PASSED_ALL=$((PASSED_ALL + passed))
    FAILED_ALL=$((FAILED_ALL + failed))
    MISSING_ALL=$((MISSING_ALL + missing_md5))
}

# -------- Run verification --------
verify_directory "$BASELINE_DIR" "Baseline"
verify_directory "$UPDATES_DIR"  "Updates"

# -------- Overall Summary --------
echo ""
echo "=================================================="
echo -e "${BOLD}OVERALL SUMMARY${NC}"
echo "=================================================="
echo "  Total files checked : $TOTAL_ALL"
echo -e "  Passed              : ${GREEN}$PASSED_ALL${NC}"
echo -e "  Failed              : ${RED}$FAILED_ALL${NC}"
echo -e "  Missing MD5         : ${YELLOW}$MISSING_ALL${NC}"
echo "=================================================="

if [[ "$FAILED_ALL" -gt 0 ]]; then
    echo -e "${RED}Some files failed checksum verification.${NC}"
    exit 1
elif [[ "$MISSING_ALL" -gt 0 ]]; then
    echo -e "${YELLOW}Verification finished with missing MD5 files.${NC}"
    exit 0
else
    echo -e "${GREEN}All available files passed checksum verification.${NC}"
    exit 0
fi
