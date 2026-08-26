#!/usr/bin/env bash

# ============================================================
# Episteme - PubMed Checksum Verification Script
# Verifies .xml.gz files against their .md5 checksums
# ============================================================

set -euo pipefail

# Default paths (can be overridden by arguments)
DATA_DIR="${1:-./01_raw/pubmed}"
BASELINE_DIR="$DATA_DIR/baseline"
UPDATES_DIR="$DATA_DIR/updates"
MD5_DIR="$DATA_DIR/md5"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=================================================="
echo "PubMed Checksum Verification"
echo "Data directory: $DATA_DIR"
echo "=================================================="

verify_directory() {
    local dir="$1"
    local label="$2"

    if [[ ! -d "$dir" ]]; then
        echo -e "${YELLOW}⚠  Directory not found: $dir — skipping $label${NC}"
        return
    fi

    echo ""
    echo "→ Verifying $label ($dir)"
    echo "--------------------------------------------------"

    local total=0
    local passed=0
    local failed=0
    local missing_md5=0

    # Find all .xml.gz files
    while IFS= read -r -d '' file; do
        ((total++))
        filename=$(basename "$file")
        md5_file="$MD5_DIR/${filename}.md5"

        # Also support md5 files sitting next to the data (fallback)
        if [[ ! -f "$md5_file" ]]; then
            md5_file="${file}.md5"
        fi

        if [[ ! -f "$md5_file" ]]; then
            echo -e "  ${YELLOW}⚠  Missing MD5: $filename${NC}"
            ((missing_md5++))
            continue
        fi

        # Run md5 check
        if md5sum -c "$md5_file" --quiet 2>/dev/null; then
            echo -e "  ${GREEN}✓${NC} $filename"
            ((passed++))
        else
            # Try a more portable approach if md5sum -c fails on some systems
            expected=$(cat "$md5_file" | awk '{print $1}')
            actual=$(md5sum "$file" | awk '{print $1}')

            if [[ "$expected" == "$actual" ]]; then
                echo -e "  ${GREEN}✓${NC} $filename"
                ((passed++))
            else
                echo -e "  ${RED}✗ FAILED: $filename${NC}"
                echo -e "      Expected: $expected"
                echo -e "      Actual:   $actual"
                ((failed++))
            fi
        fi
    done < <(find "$dir" -maxdepth 1 -name "pubmed26n*.xml.gz" -print0 | sort -z)

    echo ""
    echo "  Summary for $label:"
    echo "    Total files : $total"
    echo -e "    Passed      : ${GREEN}$passed${NC}"
    echo -e "    Failed      : ${RED}$failed${NC}"
    echo -e "    Missing MD5 : ${YELLOW}$missing_md5${NC}"
}

# -------- Run verification --------
verify_directory "$BASELINE_DIR" "Baseline"
verify_directory "$UPDATES_DIR"  "Updates"

echo ""
echo "=================================================="
echo "Verification complete."
echo "=================================================="
