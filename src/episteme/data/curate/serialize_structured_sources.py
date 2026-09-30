import argparse
import glob
import gzip
import json
import os
import xml.etree.ElementTree as ET

import pandas as pd


def process_pubmed_xml(filepath):
    """Memory-efficient streaming parser for PubMed XML.gz files."""
    articles = []
    # If file is gz, open with gzip, else open normally
    open_func = gzip.open if filepath.endswith(".gz") else open

    try:
        context = ET.iterparse(open_func(filepath, "rt", encoding="utf-8"), events=("start", "end"))
        context = iter(context)
        event, root = next(context)

        for event, elem in context:
            if event == "end" and elem.tag == "PubmedArticle":
                pmid_el = elem.find(".//PMID")
                title_el = elem.find(".//ArticleTitle")
                abstract_el = elem.find(".//AbstractText")

                pmid = pmid_el.text if pmid_el is not None else ""
                title = title_el.text if title_el is not None else ""
                abstract = abstract_el.text if abstract_el is not None else ""

                if title or abstract:
                    text_content = f"Title: {title}\nAbstract: {abstract}"
                    articles.append(
                        {
                            "id": f"pubmed_{pmid}" if pmid else "pubmed_unknown",
                            "text": text_content,
                            "source": "pubmed",
                            "license": "public_domain",
                        }
                    )
                root.clear()  # Clear memory
    except Exception as e:
        print(f"Error parsing PubMed XML {filepath}: {e}")
        # Try parsing mock/simple XML structure as fallback
        try:
            with open_func(filepath, "rt", encoding="utf-8") as f:
                tree = ET.parse(f)
                root = tree.getroot()
                for elem in root.findall(".//PubmedArticle"):
                    pmid = elem.find(".//PMID").text if elem.find(".//PMID") is not None else ""
                    title = (
                        elem.find(".//ArticleTitle").text
                        if elem.find(".//ArticleTitle") is not None
                        else ""
                    )
                    abstract = (
                        elem.find(".//AbstractText").text
                        if elem.find(".//AbstractText") is not None
                        else ""
                    )
                    text_content = f"Title: {title}\nAbstract: {abstract}"
                    articles.append(
                        {
                            "id": f"pubmed_{pmid}" if pmid else "pubmed_unknown",
                            "text": text_content,
                            "source": "pubmed",
                            "license": "public_domain",
                        }
                    )
        except Exception as ex:
            print(f"Fallback parsing also failed for {filepath}: {ex}")

    return articles


def process_uniprot_fasta(filepath):
    """Parse UniProt FASTA headers and sequences, serializing them into natural prose."""
    serialized_records = []
    current_header = ""
    current_seq = []

    open_func = gzip.open if filepath.endswith(".gz") else open

    with open_func(filepath, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith(">"):
                if current_header and current_seq:
                    serialized_records.append(
                        serialize_uniprot(current_header, "".join(current_seq))
                    )
                current_header = line
                current_seq = []
            else:
                current_seq.append(line)
        if current_header and current_seq:
            serialized_records.append(serialize_uniprot(current_header, "".join(current_seq)))

    return serialized_records


def serialize_uniprot(header, sequence):
    """Serialize UniProt header details and FASTA sequence to natural language."""
    # Header format: >sp|P68871|HBB_HUMAN Hemoglobin subunit beta OS=Homo sapiens
    # OX=9606 GN=HBB PE=1 SV=2
    try:
        parts = header[1:].split("|")
        acc = parts[1]
        rest = parts[2]

        # Parse entry name and key-value pairs
        entry_name = rest.split(" ")[0]
        desc_and_kv = rest[len(entry_name) :].strip()

        # Split on OS=, OX=, GN=, etc.
        kv_markers = ["OS=", "OX=", "GN=", "PE=", "SV="]
        kv_data = {}

        current_text = desc_and_kv
        for marker in reversed(kv_markers):
            if marker in current_text:
                current_text, val = current_text.split(marker, 1)
                kv_data[marker[:-1]] = val.strip()

        description = current_text.strip()
        organism = kv_data.get("OS", "Unknown organism")
        gene = kv_data.get("GN", "Unknown gene")

        text = (
            f"Protein {entry_name} ({description}) in organism {organism} is encoded by "
            f"gene {gene}. Its amino acid sequence is: {sequence}"
        )
        return {
            "id": f"uniprot_{acc}",
            "text": text,
            "source": "uniprot",
            "license": "CC-BY-ND-4.0",  # UniProt data license
        }
    except Exception:
        # Fallback serialization
        return {
            "id": "uniprot_unknown",
            "text": f"Protein entry {header}: sequence {sequence}",
            "source": "uniprot",
            "license": "CC-BY-ND-4.0",
        }


def process_chembl_csv(filepath):
    """Process chemical target affinity CSV/SQLite data, serializing to natural language."""
    records = []
    df = pd.read_csv(filepath)
    for idx, row in df.iterrows():
        smiles = row.get("canonical_smiles", "Unknown")
        chembl_id = row.get("chembl_id", f"chembl_row_{idx}")
        type_ = row.get("standard_type", "affinity")
        val = row.get("standard_value", "unknown")
        units = row.get("standard_units", "units")
        target_name = row.get("target_pref_name", "unknown target")

        text = (
            f"Chemical compound {chembl_id} with structure SMILES {smiles} exhibits binding "
            f"activity {type_} value of {val} {units} with target protein {target_name}."
        )
        records.append(
            {
                "id": f"chembl_{chembl_id}",
                "text": text,
                "source": "chembl",
                "license": "CC-BY-SA-3.0",
            }
        )
    return records


def filter_and_process_openmedtext(root_dir):
    """Process OpenMedText, programmatically filtering out Non-Commercial (NC) folders."""
    records = []
    print("Processing OpenMedText with strict license checking...")

    # OpenMedText layout can contain subfolders with different licenses (CC-BY vs CC-BY-NC)
    # Search for all subfolders
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        # Exclude directories with NC in name or parent paths
        if any(
            nc_indicator in dirpath.upper()
            for nc_indicator in ["-NC", "_NC", "NON-COMMERCIAL", "NONCOMMERCIAL"]
        ):
            print(f"License violation detected! Skipping non-commercial directory: {dirpath}")
            continue

        for filename in filenames:
            if filename.endswith(".txt"):
                filepath = os.path.join(dirpath, filename)
                with open(filepath, encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    records.append(
                        {
                            "id": f"openmedtext_{os.path.basename(filename)}",
                            "text": content,
                            "source": "openmedtext",
                            "license": "CC-BY",  # Confirmed open
                        }
                    )
    return records


def main():
    parser = argparse.ArgumentParser(description="Phase 0 Episteme Preprocessor & Serializer")
    parser.add_argument(
        "--input_dir",
        type=str,
        default="./data",
        help="Input folder containing downloaded raw files",
    )
    parser.add_argument(
        "--output_file",
        type=str,
        default="./data/pretrain_corpus.jsonl",
        help="Path to write the processed pretraining corpus (JSONL format)",
    )

    args = parser.parse_args()

    print(f"Input directory: {os.path.abspath(args.input_dir)}")
    print(f"Output corpus: {os.path.abspath(args.output_file)}")

    all_corpus = []

    # 1. Process PubMed Baseline
    pubmed_dir = os.path.join(args.input_dir, "pubmed")
    if os.path.exists(pubmed_dir):
        xml_files = glob.glob(os.path.join(pubmed_dir, "*.xml*"))
        print(f"Found {len(xml_files)} PubMed XML files to process.")
        for xml_file in xml_files:
            all_corpus.extend(process_pubmed_xml(xml_file))

    # 2. Process UniProt FASTA
    uniprot_dir = os.path.join(args.input_dir, "uniprot")
    if os.path.exists(uniprot_dir):
        fasta_files = glob.glob(os.path.join(uniprot_dir, "*.fasta*"))
        print(f"Found {len(fasta_files)} UniProt FASTA files to process.")
        for fasta_file in fasta_files:
            all_corpus.extend(process_uniprot_fasta(fasta_file))

    # 3. Process ChEMBL CSV
    chembl_dir = os.path.join(args.input_dir, "chembl")
    if os.path.exists(chembl_dir):
        csv_files = glob.glob(os.path.join(chembl_dir, "*.csv"))
        print(f"Found {len(csv_files)} ChEMBL CSV files to process.")
        for csv_file in csv_files:
            all_corpus.extend(process_chembl_csv(csv_file))

    # 4. Process OpenMedText & Filter Licenses
    openmedtext_dir = os.path.join(args.input_dir, "hf", "openmedtext")
    # For testing, we also check general HF folder
    if not os.path.exists(openmedtext_dir):
        openmedtext_dir = os.path.join(args.input_dir, "openmedtext")
    if os.path.exists(openmedtext_dir):
        all_corpus.extend(filter_and_process_openmedtext(openmedtext_dir))

    # Write entire corpus to output JSONL file
    os.makedirs(os.path.dirname(args.output_file), exist_ok=True)
    with open(args.output_file, "w", encoding="utf-8") as f:
        for record in all_corpus:
            f.write(json.dumps(record) + "\n")

    print(f"Preprocessing completed. Total records serialized/gathered: {len(all_corpus)}")


if __name__ == "__main__":
    main()
