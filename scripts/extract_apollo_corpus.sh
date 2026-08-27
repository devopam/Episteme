#!/usr/bin/env bash
# Episteme – Extract ApolloCorpus → JSONL
set -euo pipefail

# Resolve project root (assumes script lives in scripts/data/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

INPUT_DIR="${1:-$PROJECT_ROOT/01_raw/multilingual/apollo/raw}"
OUTPUT_JSONL="${2:-$PROJECT_ROOT/02_processed/multilingual/apollo/apollo_pretrain.jsonl}"
MIN_CHARS="${3:-50}"

echo "=================================================="
echo "ApolloCorpus Extractor"
echo "Input  : $INPUT_DIR"
echo "Output : $OUTPUT_JSONL"
echo "=================================================="

mkdir -p "$(dirname "$OUTPUT_JSONL")"

export PYTHONPATH="${PROJECT_ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"

python -m episteme.data.apollo.extract \
  --input-dir "$INPUT_DIR" \
  --output-jsonl "$OUTPUT_JSONL" \
  --min-chars "$MIN_CHARS"

echo "Done."
