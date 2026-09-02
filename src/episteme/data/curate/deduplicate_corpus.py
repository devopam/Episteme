import os
import sys
import json
import argparse
from datasketch import MinHash, MinHashLSH
from tqdm import tqdm
import re


def get_shingles(text, n=3):
    """Generate word n-grams/shingles from text."""
    # Clean text to alphanumeric lowercase words
    words = re.findall(r'\b\w+\b', text.lower())
    if len(words) < n:
        return set(words)
    shingles = set()
    for i in range(len(words) - n + 1):
        shingle = " ".join(words[i:i+n])
        shingles.add(shingle)
    return shingles


def build_minhash(shingles, num_perm=128):
    """Build a MinHash signature for a set of shingles."""
    m = MinHash(num_perm=num_perm)
    for shingle in shingles:
        m.update(shingle.encode('utf-8'))
    return m


def deduplicate_corpus(input_path, output_path, threshold=0.8, num_perm=128):
    """Near-deduplicate corpus using MinHash LSH."""
    print(f"=== Starting MinHash LSH Deduplication ===")
    print(f"Input: {input_path}")
    print(f"Threshold Jaccard similarity: {threshold}")
    
    lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
    
    total_records = 0
    duplicate_count = 0
    unique_records = []
    
    # Read all records first
    records = []
    if not os.path.exists(input_path):
        print(f"Input file {input_path} does not exist. Creating a small mock file for verification.")
        os.makedirs(os.path.dirname(input_path), exist_ok=True)
        # Create small mock file
        mock_data = [
            {"id": "doc1", "text": "A standard clinical study showing acetylcholinesterase binding compound CHEMBL25.", "source": "chembl", "license": "CC-BY-SA-3.0"},
            {"id": "doc2", "text": "A standard clinical study showing acetylcholinesterase binding compound CHEMBL25. (Near duplicate)", "source": "chembl", "license": "CC-BY-SA-3.0"},
            {"id": "doc3", "text": "Protein HBB_HUMAN sequence details and functions.", "source": "uniprot", "license": "CC-BY-ND-4.0"}
        ]
        with open(input_path, "w", encoding="utf-8") as f:
            for rec in mock_data:
                f.write(json.dumps(rec) + "\n")
                
    with open(input_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
                
    total_records = len(records)
    print(f"Loaded {total_records} records from file.")

    for record in tqdm(records, desc="Deduplicating"):
        doc_id = record.get("id", "unknown_id")
        text = record.get("text", "")
        
        shingles = get_shingles(text)
        if not shingles:
            # If no words, keep it but skip hash index
            unique_records.append(record)
            continue
            
        m = build_minhash(shingles, num_perm)
        
        # Query LSH
        results = lsh.query(m)
        if results:
            duplicate_count += 1
        else:
            # Insert into LSH and save
            lsh.insert(doc_id, m)
            unique_records.append(record)
            
    print(f"Deduplication summary:")
    print(f"Total processed: {total_records}")
    print(f"Duplicates skipped: {duplicate_count}")
    print(f"Unique saved: {len(unique_records)}")
    
    # Save output corpus
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for rec in unique_records:
            f.write(json.dumps(rec) + "\n")
            
    print(f"Deduplicated corpus saved to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Phase 0 MinHash LSH Deduplication CLI")
    parser.add_argument(
        "--input_file",
        type=str,
        default="./data/pretrain_corpus.jsonl",
        help="Input JSONL pretrain corpus file"
    )
    parser.add_argument(
        "--output_file",
        type=str,
        default="./data/pretrain_corpus_dedup.jsonl",
        help="Output JSONL deduplicated file"
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.8,
        help="Jaccard similarity threshold for near-duplicate detection (0.0 to 1.0)"
    )
    parser.add_argument(
        "--num_perm",
        type=int,
        default=128,
        help="Number of permutations for MinHash signatures"
    )

    args = parser.parse_args()
    deduplicate_corpus(
        args.input_file,
        args.output_file,
        args.threshold,
        args.num_perm
    )


if __name__ == "__main__":
    main()
