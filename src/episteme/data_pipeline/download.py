import os
import sys
import argparse
import urllib.request
import ftplib
import hashlib
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm
import requests
from datasets import load_dataset

# We import pyeuropepmc dynamically if needed, since it might not be fully configured in all test setups
try:
    import pyeuropepmc
except ImportError:
    pyeuropepmc = None


def get_md5(filepath):
    """Calculate MD5 checksum of a file."""
    hash_md5 = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def download_file(url, output_path, dry_run=False):
    """Download a file with resume support and progress bar."""
    if dry_run:
        print(f"[DRY-RUN] Would download: {url} -> {output_path}")
        return True

    temp_path = output_path + ".tmp"
    headers = {}
    
    # Check if a partial download exists to resume
    file_exists = os.path.exists(output_path)
    temp_exists = os.path.exists(temp_path)
    
    if file_exists:
        print(f"File already exists: {output_path}")
        return True

    resume_header = {}
    existing_size = 0
    if temp_exists:
        existing_size = os.path.getsize(temp_path)
        resume_header = {'Range': f'bytes={existing_size}-'}
        print(f"Resuming download of {output_path} from byte {existing_size}")

    try:
        response = requests.get(url, headers=resume_header, stream=True, timeout=30)
        
        # If server does not support range queries, restart download
        mode = 'ab' if (response.status_code == 206 and temp_exists) else 'wb'
        if mode == 'wb':
            existing_size = 0

        total_size = int(response.headers.get('content-length', 0)) + existing_size
        
        with open(temp_path, mode) as f, tqdm(
            desc=os.path.basename(output_path),
            total=total_size,
            unit='iB',
            unit_scale=True,
            unit_divisor=1024,
            initial=existing_size
        ) as bar:
            for data in response.iter_content(chunk_size=8192):
                size = f.write(data)
                bar.update(size)
                
        os.replace(temp_path, output_path)
        return True
    except Exception as e:
        print(f"Error downloading {url}: {e}")
        return False


def download_pubmed(output_dir, num_threads=4, dry_run=False, sample_only=False):
    """Download PubMed baseline XML files via FTP."""
    print("=== Downloading PubMed Baseline ===")
    os.makedirs(output_dir, exist_ok=True)
    ftp_host = "ftp.ncbi.nlm.nih.gov"
    ftp_path = "pubmed/baseline"
    
    if dry_run:
        print(f"[DRY-RUN] Would connect to {ftp_host}/{ftp_path} and download XML gz files.")
        return
        
    try:
        ftp = ftplib.FTP(ftp_host)
        ftp.login()
        ftp.cwd(ftp_path)
        files = [f for f in ftp.nlst() if f.endswith(".xml.gz")]
        ftp.quit()
    except Exception as e:
        print(f"Failed to connect to PubMed FTP: {e}. Falling back to sample files.")
        files = ["pubmed26n0001.xml.gz"]  # Fallback sample list
        if sample_only:
            # Generate local mock file for testing
            mock_file = os.path.join(output_dir, files[0])
            with open(mock_file, "w") as f:
                f.write("<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>1</PMID><Article><ArticleTitle>Sample title</ArticleTitle><Abstract><AbstractText>Sample abstract text</AbstractText></Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>")
            print(f"Created mock sample PubMed file at {mock_file}")
            return

    if sample_only:
        files = files[:2]
        
    urls = [f"https://{ftp_host}/{ftp_path}/{f}" for f in files]
    
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = []
        for url, filename in zip(urls, files):
            out_path = os.path.join(output_dir, filename)
            futures.append(executor.submit(download_file, url, out_path, dry_run))
        for f in futures:
            f.result()


def download_pmc_oa(output_dir, num_threads=4, dry_run=False, sample_only=False):
    """Download PMC Open Access commercial subset."""
    print("=== Downloading PMC Open Access Commercial Subset ===")
    os.makedirs(output_dir, exist_ok=True)
    # The list of commercial use bulk files
    bulk_url = "https://ftp.ncbi.nlm.nih.gov/pub/pmc/oa_bulk/oa_comm/xml/"
    
    # In a full run, we would parse the index.html or ftp to get all zip packages.
    # For now, we fetch a few sample packages or document the bulk URLs.
    packages = ["oa_comm_xml_metadata.csv.gz", "oa_comm_xml_pdf_01.tar.gz"]
    if sample_only:
        packages = ["oa_comm_xml_metadata.csv.gz"]
        
    for pkg in packages:
        url = f"{bulk_url}{pkg}"
        out_path = os.path.join(output_dir, pkg)
        download_file(url, out_path, dry_run)


def download_europe_pmc(output_dir, dry_run=False, sample_only=False):
    """Download Europe PMC preprint metadata/XML."""
    print("=== Downloading Europe PMC Preprints ===")
    os.makedirs(output_dir, exist_ok=True)
    if pyeuropepmc is None:
        print("pyeuropepmc is not installed. Skipping direct API query, using fallback requests.")
        # Fallback to direct HTTP request to Europe PMC REST API
        url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=SRC:PPR%20AND%20(COVID-19%20OR%20SARS-CoV-2)&format=json"
        if not dry_run:
            response = requests.get(url, timeout=30)
            if response.status_code == 200:
                with open(os.path.join(output_dir, "europe_pmc_sample.json"), "w", encoding="utf-8") as f:
                    f.write(response.text)
                print("Downloaded Europe PMC REST query sample.")
        else:
            print(f"[DRY-RUN] Would fetch from: {url}")
    else:
        # Example pyeuropepmc query
        print("Using pyeuropepmc to search for preprints...")
        if dry_run:
            print("[DRY-RUN] Would call pyeuropepmc.search()")
        else:
            try:
                results = pyeuropepmc.search("SRC:PPR AND (COVID-19 OR SARS-CoV-2)", limit=10)
                # Save results
                import json
                with open(os.path.join(output_dir, "europe_pmc_preprints.json"), "w", encoding="utf-8") as f:
                    json.dump(list(results), f, indent=2)
                print("Europe PMC preprints fetched and saved.")
            except Exception as e:
                print(f"Europe PMC query failed: {e}")


def download_chembl(output_dir, dry_run=False, sample_only=False):
    """Download ChEMBL database release SQLite dump and SureChEMBL sample."""
    print("=== Downloading ChEMBL Database ===")
    os.makedirs(output_dir, exist_ok=True)
    
    # SQLite version is lightweight compared to PostgreSQL and perfect for local pipelines
    chembl_version = "34"
    url = f"ftp://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/releases/chembl_{chembl_version}/chembl_{chembl_version}_sqlite.tar.gz"
    
    if sample_only:
        print("Sample only requested. Writing mock ChEMBL structures for pipeline verification.")
        mock_db_path = os.path.join(output_dir, "mock_chembl.csv")
        with open(mock_db_path, "w", encoding="utf-8") as f:
            f.write("chembl_id,canonical_smiles,standard_type,standard_value,standard_units,target_chembl_id,target_pref_name\n")
            f.write("CHEMBL25,CHEMBL25_SMILES,IC50,5.4,nM,CHEMBL1827,Acetylcholinesterase\n")
            f.write("CHEMBL123,CHEMBL123_SMILES,Ki,10.2,uM,CHEMBL1828,Butyrylcholinesterase\n")
        print(f"Created mock ChEMBL file at {mock_db_path}")
        return

    # In regular operation, we would download the tar.gz file
    out_path = os.path.join(output_dir, f"chembl_{chembl_version}_sqlite.tar.gz")
    download_file(url.replace("ftp://", "https://"), out_path, dry_run)


def download_uniprot(output_dir, dry_run=False, sample_only=False):
    """Download UniProt (Swiss-Prot / TrEMBL) datasets."""
    print("=== Downloading UniProt Database ===")
    os.makedirs(output_dir, exist_ok=True)
    
    swiss_prot_url = "https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/complete/uniprot_sprot.fasta.gz"
    
    if sample_only:
        print("Sample only requested. Writing mock UniProt FASTA file for pipeline verification.")
        mock_fasta = os.path.join(output_dir, "mock_uniprot_sprot.fasta")
        with open(mock_fasta, "w", encoding="utf-8") as f:
            f.write(">sp|P68871|HBB_HUMAN Hemoglobin subunit beta OS=Homo sapiens OX=9606 GN=HBB PE=1 SV=2\n")
            f.write("VHLTPEEKSAVTALWGKVNVDEVGGEALGRLLVVYPWTQRFFESFGDLSTPDAVMGNPKV\n")
            f.write("KAHGKKVLGAFSDGLAHLDNLKGTFATLSELHCDKLHVDPENFRLLGNVLVCVLAHHFGK\n")
            f.write("EFTPPVQAAYQKVVAGVANALAHKYH\n")
        print(f"Created mock UniProt FASTA file at {mock_fasta}")
        return

    out_path = os.path.join(output_dir, "uniprot_sprot.fasta.gz")
    download_file(swiss_prot_url, out_path, dry_run)


def download_hf_datasets(output_dir, dry_run=False, sample_only=False):
    """Download EPFL Meditron Guidelines, MedMCQA, and PubMedQA datasets from Hugging Face."""
    print("=== Downloading Hugging Face Datasets ===")
    os.makedirs(output_dir, exist_ok=True)
    
    datasets_to_fetch = {
        "guidelines": "epfl-llm/guidelines",
        "pubmedqa": "qiaojin/pubmedqa",
        "medmcqa": "openlifescienceai/medmcqa"
    }
    
    if dry_run:
        for name, path in datasets_to_fetch.items():
            print(f"[DRY-RUN] Would download dataset: {path} and save to {output_dir}/{name}")
        return
        
    for name, path in datasets_to_fetch.items():
        print(f"Downloading HF dataset: {path}")
        try:
            if name == "pubmedqa":
                # PubMedQA requires config name
                dataset = load_dataset(path, "pqa_labeled")
            else:
                dataset = load_dataset(path)
            
            # Save local copies
            dataset.save_to_disk(os.path.join(output_dir, name))
            print(f"Successfully saved {path} to disk.")
        except Exception as e:
            print(f"Failed to download {path}: {e}")
            if sample_only:
                print(f"Creating mock dataset structure for {name}")
                # Create a small mock directory
                mock_dir = os.path.join(output_dir, name)
                os.makedirs(mock_dir, exist_ok=True)
                with open(os.path.join(mock_dir, "mock_data.txt"), "w") as f:
                    f.write("Mock dataset content for testing.")


def main():
    parser = argparse.ArgumentParser(description="Phase 0 Episteme Data Downloader CLI")
    parser.add_argument(
        "--dataset",
        type=str,
        choices=["all", "pubmed", "pmc", "europe_pmc", "chembl", "uniprot", "hf"],
        default="all",
        help="Target dataset to download"
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="./data",
        help="Directory to save downloaded files"
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=4,
        help="Number of concurrent download threads"
    )
    parser.add_argument(
        "--dry_run",
        action="store_true",
        help="Simulate download process without fetching files"
    )
    parser.add_argument(
        "--sample_only",
        action="store_true",
        default=True,
        help="Download/create small sample datasets for validation purposes (default is True to save space/time)"
    )

    args = parser.parse_args()
    
    print(f"Output directory set to: {os.path.abspath(args.output_dir)}")
    
    if args.dataset in ["all", "pubmed"]:
        download_pubmed(os.path.join(args.output_dir, "pubmed"), args.threads, args.dry_run, args.sample_only)
        
    if args.dataset in ["all", "pmc"]:
        download_pmc_oa(os.path.join(args.output_dir, "pmc"), args.threads, args.dry_run, args.sample_only)
        
    if args.dataset in ["all", "europe_pmc"]:
        download_europe_pmc(os.path.join(args.output_dir, "europe_pmc"), args.dry_run, args.sample_only)
        
    if args.dataset in ["all", "chembl"]:
        download_chembl(os.path.join(args.output_dir, "chembl"), args.dry_run, args.sample_only)
        
    if args.dataset in ["all", "uniprot"]:
        download_uniprot(os.path.join(args.output_dir, "uniprot"), args.dry_run, args.sample_only)
        
    if args.dataset in ["all", "hf"]:
        download_hf_datasets(os.path.join(args.output_dir, "hf"), args.dry_run, args.sample_only)

    print("Data download step complete.")


if __name__ == "__main__":
    main()
