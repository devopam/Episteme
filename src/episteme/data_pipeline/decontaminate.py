import os
import sys
import json
import argparse
import re
from datasets import load_dataset
from tqdm import tqdm


def normalize_text(text):
    """Normalize text by lowercasing and removing non-alphanumeric characters and extra spaces."""
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    words = text.split()
    return words


def get_ngrams(words, n=13):
    """Generate n-grams from a list of words."""
    if len(words) < n:
        return set()
    ngrams = set()
    for i in range(len(words) - n + 1):
        ngrams.add(tuple(words[i:i+n]))
    return ngrams


def build_test_ngrams(ngram_size=13, sample_only=False):
    """Load test sets from HF and build a set of normalized n-grams."""
    print("=== Building Evaluation Test Set N-Grams ===")
    test_ngrams = set()
    
    # Target evaluation datasets
    datasets_info = [
        {"path": "openlifescienceai/medmcqa", "split": "test", "fields": ["question", "opa", "opb", "opc", "opd"]},
        {"path": "qiaojin/pubmedqa", "name": "pqa_labeled", "split": "train", "fields": ["question", "context"]} # use train since it is QA pairs
    ]
    
    if sample_only:
        print("Sample/Mock mode enabled. Using mock test set questions.")
        mock_questions = [
            "What is the standard treatment for acute acetylcholinesterase poisoning?",
            "What amino acid sequence is associated with Hemoglobin subunit beta?",
            "What are the genomic variants of BRCA1 associated with disease?"
        ]
        for q in mock_questions:
            words = normalize_text(q)
            for ngram in get_ngrams(words, ngram_size):
                test_ngrams.add(ngram)
        print(f"Built {len(test_ngrams)} n-grams from mock test questions.")
        return test_ngrams

    for info in datasets_info:
        path = info["path"]
        name = info.get("name", None)
        split = info["split"]
        fields = info["fields"]
        
        print(f"Loading {path} ({split} split)...")
        try:
            if name:
                dataset = load_dataset(path, name, split=split)
            else:
                dataset = load_dataset(path, split=split)
                
            for row in tqdm(dataset, desc=f"Hashing {os.path.basename(path)}"):
                # Combine all specified fields into a single block of text
                combined_text = ""
                for field in fields:
                    val = row.get(field, "")
                    if isinstance(val, list):
                        val = " ".join(val)
                    if val:
                        combined_text += " " + str(val)
                        
                words = normalize_text(combined_text)
                for ngram in get_ngrams(words, ngram_size):
                    test_ngrams.add(ngram)
        except Exception as e:
            print(f"Could not load test dataset {path}: {e}. Skipping it.")
            
    print(f"Finished building test set database. Total unique test n-grams: {len(test_ngrams)}")
    return test_ngrams


def decontaminate_corpus(input_path, output_path, ngram_size=13, sample_only=False):
    """Filter out documents from pretraining corpus that contain test set n-grams."""
    test_ngrams = build_test_ngrams(ngram_size, sample_only)
    
    if not test_ngrams:
        print("No test n-grams found. Copying input file to output directly.")
        if os.path.exists(input_path):
            import shutil
            shutil.copy(input_path, output_path)
        return
        
    print(f"=== Running Decontamination ===")
    print(f"Input: {input_path}")
    print(f"Output: {output_path}")
    
    clean_records = []
    contaminated_count = 0
    total_count = 0
    
    if not os.path.exists(input_path):
        print(f"Input file {input_path} does not exist. Skipping decontamination loop.")
        return

    with open(input_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            total_count += 1
            record = json.loads(line)
            text = record.get("text", "")
            
            words = normalize_text(text)
            doc_ngrams = get_ngrams(words, ngram_size)
            
            # Check intersection
            is_contaminated = False
            for ngram in doc_ngrams:
                if ngram in test_ngrams:
                    is_contaminated = True
                    break
                    
            if is_contaminated:
                contaminated_count += 1
            else:
                clean_records.append(record)
                
    print(f"Decontamination Summary:")
    print(f"Total documents processed: {total_count}")
    print(f"Contaminated documents removed: {contaminated_count}")
    print(f"Clean documents retained: {len(clean_records)}")
    
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for rec in clean_records:
            f.write(json.dumps(rec) + "\n")
            
    print(f"Decontaminated corpus written to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Phase 0 Benchmark Decontamination CLI")
    parser.add_argument(
        "--input_file",
        type=str,
        default="./data/pretrain_corpus_dedup.jsonl",
        help="Input JSONL pretrain corpus file"
    )
    parser.add_argument(
        "--output_file",
        type=str,
        default="./data/pretrain_corpus_clean.jsonl",
        help="Output JSONL clean decontaminated file"
    )
    parser.add_argument(
        "--ngram_size",
        type=int,
        default=13,
        help="N-gram window size for decontamination check"
    )
    parser.add_argument(
        "--sample_only",
        action="store_true",
        default=True,
        help="Enable mock test questions to skip downloading full datasets (default is True to save time)"
    )

    args = parser.parse_args()
    decontaminate_corpus(
        args.input_file,
        args.output_file,
        args.ngram_size,
        args.sample_only
    )


if __name__ == "__main__":
    main()
