#!/usr/bin/env python3
"""
Episteme – PMC Commercial OA (oa_comm) Downloader
Uses NCBI ESearch to get commercial license PMCID list
and downloads XML/TXT/metadata directly from pmc-oa-opendata S3 bucket via HTTPS.
"""

import os
import sys
import argparse
import urllib.parse
import urllib.request
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from tqdm import tqdm

DEFAULT_QUERY = (
    "((cc0_license[filter] OR cc_by_license[filter] OR cc_by-sa_license[filter] "
    "OR cc_by-nd_license[filter]) OR author_manuscript[filter]) NOT pmc_embargo[filter]"
)

S3_BASE_URL = "https://pmc-oa-opendata.s3.amazonaws.com"


def query_esearch(query_str, limit=0):
    """Query NCBI ESearch to get list of PMCIDs matching query."""
    print(f"Querying NCBI ESearch for: {query_str}")
    base_url = "https://eutils.ncbi.nlm.nih.gov/eutils/esearch.fcgi"
    
    # ESearch limit cap is 10,000 for standard queries.
    # If limit is 0 (all), we fetch up to 10,000. If more is needed,
    # we would need pagination, but 10,000 is a large baseline batch.
    retmax = limit if (limit > 0 and limit <= 10000) else 10000
    
    params = {
        "db": "pmc",
        "term": query_str,
        "retmode": "json",
        "retmax": retmax
    }
    
    encoded_params = urllib.parse.urlencode(params)
    url = f"{base_url}?{encoded_params}"
    
    # Retry on failures
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=30)
            if response.status_code == 200:
                data = response.json()
                id_list = data.get("esearchresult", {}).get("idlist", [])
                print(f"Found {len(id_list)} matching articles.")
                if limit > 0:
                    id_list = id_list[:limit]
                return id_list
            else:
                print(f"ESearch returned status code {response.status_code}")
        except Exception as e:
            print(f"Attempt {attempt + 1} failed: {e}")
        time.sleep(2)
        
    return []


def get_metadata(pmcid):
    """
    Find and download the metadata JSON for the given PMCID.
    Tries version 1, then version 2.
    """
    for version in [1, 2, 3]:
        meta_url = f"{S3_BASE_URL}/metadata/PMC{pmcid}.{version}.json"
        try:
            res = requests.get(meta_url, timeout=10)
            if res.status_code == 200:
                return res.json(), f"PMC{pmcid}.{version}"
        except Exception:
            pass
    return None, None


def download_url_to_file(url, out_path, dry_run=False):
    """Download a single URL to a file path."""
    if dry_run:
        return True
        
    temp_path = out_path + ".tmp"
    try:
        response = requests.get(url, stream=True, timeout=20)
        if response.status_code == 200:
            with open(temp_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            os.replace(temp_path, out_path)
            return True
    except Exception as e:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise e
    return False


def process_article(pmcid, output_dir, formats, dry_run=False):
    """Download formats and metadata for a single PMCID."""
    meta, version_id = get_metadata(pmcid)
    if not meta:
        return pmcid, False, "Metadata not found on S3"
        
    # Create target directories
    os.makedirs(os.path.join(output_dir, "metadata"), exist_ok=True)
    for fmt in formats:
        os.makedirs(os.path.join(output_dir, fmt), exist_ok=True)
        
    # Write metadata JSON
    meta_path = os.path.join(output_dir, "metadata", f"{version_id}.json")
    if not dry_run:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
            
    # Download formats
    for fmt in formats:
        url_key = f"{fmt}_url"
        s3_url = meta.get(url_key)
        if not s3_url:
            continue
            
        # Convert S3 URL to HTTP URL
        # s3://pmc-oa-opendata/PMC10000000.1/PMC10000000.1.xml?md5=...
        # -> https://pmc-oa-opendata.s3.amazonaws.com/PMC10000000.1/PMC10000000.1.xml
        path_part = s3_url.replace("s3://pmc-oa-opendata/", "").split("?")[0]
        http_url = f"{S3_BASE_URL}/{path_part}"
        
        file_ext = "txt" if fmt == "text" else fmt
        out_file = os.path.join(output_dir, fmt, f"{version_id}.{file_ext}")
        
        if os.path.exists(out_file):
            continue  # Skip already downloaded files
            
        try:
            download_url_to_file(http_url, out_file, dry_run)
        except Exception as e:
            return pmcid, False, f"Failed to download {fmt}: {e}"
            
    return pmcid, True, None


def download_pmc_commercial(output_dir, formats=['xml'], limit=10, threads=4, dry_run=False, query=None):
    """Interface function to download commercial OA articles."""
    if query is None:
        query = DEFAULT_QUERY
        
    # Normalize formats (legacy scripts passed 'xml txt')
    normalized_formats = []
    for f in formats:
        # split in case space-separated string was passed
        for part in f.split():
            if part == "txt":
                normalized_formats.append("text")
            else:
                normalized_formats.append(part)
                
    pmcids = query_esearch(query, limit)
    if not pmcids:
        print("No articles to download.")
        return 0
        
    print(f"Starting download of {len(pmcids)} articles with {threads} threads...")
    
    success_count = 0
    failures = []
    
    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {
            executor.submit(process_article, pmcid, output_dir, normalized_formats, dry_run): pmcid
            for pmcid in pmcids
        }
        
        for future in tqdm(as_completed(futures), total=len(futures), desc="Downloading PMC articles"):
            pmcid = futures[future]
            try:
                pid, success, err = future.result()
                if success:
                    success_count += 1
                else:
                    failures.append((pid, err))
            except Exception as e:
                failures.append((pmcid, str(e)))
                
    print(f"\nDownload completed: {success_count} succeeded, {len(failures)} failed.")
    if failures:
        print("Failures:")
        for pid, err in failures[:10]:
            print(f"  PMC{pid}: {err}")
        if len(failures) > 10:
            print(f"  ... and {len(failures) - 10} more failures.")
            
    # Record metadata
    if not dry_run:
        meta_summary_path = os.path.join(output_dir, "sync_meta.txt")
        os.makedirs(output_dir, exist_ok=True)
        with open(meta_summary_path, "w") as f:
            f.write("subset=oa_comm\n")
            f.write(f"formats={' '.join(formats)}\n")
            f.write(f"source={S3_BASE_URL}/\n")
            f.write(f"synced_utc={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
            
    return success_count


def main():
    parser = argparse.ArgumentParser(description="PMC Commercial OA Downloader")
    parser.add_argument(
        "--output_dir",
        type=str,
        default="./01_raw/pmc/oa_comm",
        help="Directory to save files"
    )
    parser.add_argument(
        "--formats",
        type=str,
        nargs="+",
        default=["xml"],
        help="Formats to download (xml and/or txt)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Limit number of articles to download (0 = unlimited)"
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=4,
        help="Number of download threads"
    )
    parser.add_argument(
        "--dry_run",
        action="store_true",
        help="Dry run mode"
    )
    parser.add_argument(
        "--query",
        type=str,
        default=DEFAULT_QUERY,
        help="NCBI ESearch query"
    )
    
    args = parser.parse_args()
    
    download_pmc_commercial(
        output_dir=args.output_dir,
        formats=args.formats,
        limit=args.limit,
        threads=args.threads,
        dry_run=args.dry_run,
        query=args.query
    )


if __name__ == "__main__":
    main()
