#!/usr/bin/env bash
# Episteme – Extract ApolloCorpus → JSONL
# Automatically unzips ApolloCorpus.zip if present and not yet extracted.
set -euo pipefail

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

if [[ ! -d "$INPUT_DIR" ]]; then
  echo "Error: input directory not found: $INPUT_DIR"
  exit 1
fi

# ---- Auto-unzip if needed ----
ZIP_FILE="$INPUT_DIR/ApolloCorpus.zip"
# Heuristic: extraction is needed if zip exists and no *_text.json found yet
if [[ -f "$ZIP_FILE" ]]; then
  TEXT_COUNT=$(find "$INPUT_DIR" -type f -name '*_text.json' 2>/dev/null | wc -l | tr -d ' ')
  if [[ "$TEXT_COUNT" -eq 0 ]]; then
    echo "→ Found ApolloCorpus.zip and no extracted text files."
    echo "→ Unzipping (this may take a few minutes)..."
    unzip -q -o "$ZIP_FILE" -d "$INPUT_DIR"
    echo "→ Unzip complete."
  else
    echo "→ ApolloCorpus.zip present, but text files already found — skipping unzip."
  fi
fi

mkdir -p "$(dirname "$OUTPUT_JSONL")"

export PYTHONPATH="${PROJECT_ROOT}/src${PYTHONPATH:+:$PYTHONPATH}"

python -m episteme.data.apollo.extract \
  --input-dir "$INPUT_DIR" \
  --output-jsonl "$OUTPUT_JSONL" \
  --min-chars "$MIN_CHARS"

echo "Done."
