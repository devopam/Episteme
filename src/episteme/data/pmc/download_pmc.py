#!/usr/bin/env python3
"""
Episteme – PMC Commercial OA Downloader (post Aug 2026 layout)

Source of truth for commercial reuse:
  - Prefer official oa_comm filelist if present on the bucket
  - Else NCBI ESearch with commercial license filters only
  - Always verify license_code from metadata JSON before saving

Live object layout (as of 2026-08):
  https://pmc-oa-opendata.s3.amazonaws.com/metadata/PMC{id}.{ver}.json
  https://pmc-oa-opendata.s3.amazonaws.com/PMC{id}.{ver}/PMC{id}.{ver}.xml
  (xml_url / text_url inside metadata JSON)

Commercial license_code values kept:
  CC0, CC BY, CC BY-SA, CC BY-ND
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

import requests
from tqdm import tqdm

from episteme.config import get_settings

S3_HTTP = "https://pmc-oa-opendata.s3.amazonaws.com"

# Documented (may return 404 after Aug 2026 cutover) — tried first
FILELIST_CANDIDATES = [
    f"{S3_HTTP}/oa_comm/xml/metadata/csv/oa_comm.filelist.csv",
    f"{S3_HTTP}/oa_comm/xml/metadata/txt/oa_comm.filelist.txt",
    f"{S3_HTTP}/oa_comm/xml/metadata/oa_comm.filelist.csv",
]

# Commercial-only: no author_manuscript, no NC licenses
COMMERCIAL_ESEARCH = (
    "(cc0_license[filter] OR cc_by_license[filter] OR "
    "cc_by-sa_license[filter] OR cc_by-nd_license[filter]) "
    "NOT pmc_embargo[filter]"
)

COMMERCIAL_LICENSE_CODES = {
    "CC0",
    "CC BY",
    "CC BY-SA",
    "CC BY-ND",
    "CC0 1.0",
    "CC BY 4.0",
    "CC BY-SA 4.0",
    "CC BY-ND 4.0",
}


def http_get(
    url: str,
    *,
    timeout: int = 60,
    stream: bool = False,
    retries: int = 3,
) -> requests.Response | None:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=timeout, stream=stream)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(1.5 * (attempt + 1))
    if last_err:
        print(f"  warn: GET failed {url}: {last_err}", file=sys.stderr)
    return None


def try_download_filelist(dest_dir: Path) -> Path | None:
    """Download oa_comm filelist if still published; else return None."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for url in FILELIST_CANDIDATES:
        print(f"Probing filelist: {url}")
        r = http_get(url, timeout=120)
        if r is None:
            continue
        name = url.rsplit("/", 1)[-1]
        out = dest_dir / name
        out.write_bytes(r.content)
        print(f"  saved filelist → {out} ({out.stat().st_size:,} bytes)")
        return out
    print("No oa_comm filelist found on bucket (expected after Aug 2026 layout change).")
    return None


def parse_filelist(path: Path) -> list[dict[str, str]]:
    """Parse CSV or TSV filelist into row dicts."""
    text = path.read_text(encoding="utf-8", errors="replace")
    dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t")
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    rows: list[dict[str, str]] = []
    for row in reader:
        # Normalise keys
        norm = { (k or "").strip(): (v or "").strip() for k, v in row.items() }
        rows.append(norm)
    return rows


def accession_from_filelist_row(row: dict[str, str]) -> str | None:
    for key in ("AccessionID", "Accession ID", "pmcid", "PMCID"):
        if key in row and row[key]:
            acc = row[key].strip()
            if not acc.upper().startswith("PMC"):
                acc = f"PMC{acc}"
            return acc
    # Fallback: parse Key column e.g. oa_comm/xml/all/PMC1043859.xml
    key = row.get("Key") or row.get("key") or ""
    base = key.rsplit("/", 1)[-1]
    if base.upper().startswith("PMC") and base.lower().endswith(".xml"):
        return base[:-4]
    return None


def esearch_commercial_ids(limit: int, api_key: str | None = None) -> list[str]:
    """
    Paginate NCBI ESearch for commercial-license PMC articles.
    Returns numeric id strings (no PMC prefix), as returned by ESearch.
    """
    base = "https://eutils.ncbi.nlm.nih.gov/eutils/esearch.fcgi"
    page_size = 10000
    ids: list[str] = []

    # First call with history
    params: dict[str, Any] = {
        "db": "pmc",
        "term": COMMERCIAL_ESEARCH,
        "retmode": "json",
        "retmax": min(page_size, limit) if limit > 0 else page_size,
        "usehistory": "y",
    }
    if api_key:
        params["api_key"] = api_key

    print(f"ESearch commercial query:\n  {COMMERCIAL_ESEARCH}")
    r = http_get(f"{base}?{urlencode(params)}", timeout=60)
    if r is None:
        print("ERROR: ESearch failed", file=sys.stderr)
        return []

    data = r.json().get("esearchresult", {})
    total = int(data.get("count", 0))
    webenv = data.get("webenv")
    query_key = data.get("querykey")
    batch = data.get("idlist") or []
    ids.extend(batch)
    print(f"  total matching (NCBI count): {total:,}")
    print(f"  fetched so far: {len(ids):,}")

    if limit > 0:
        target = min(limit, total)
    else:
        target = total

    retstart = len(ids)
    while retstart < target and webenv and query_key:
        time.sleep(0.34 if api_key else 0.4)  # be polite to NCBI
        n = min(page_size, target - retstart)
        params = {
            "db": "pmc",
            "query_key": query_key,
            "WebEnv": webenv,
            "retmode": "json",
            "retstart": retstart,
            "retmax": n,
        }
        if api_key:
            params["api_key"] = api_key
        r = http_get(f"{base}?{urlencode(params)}", timeout=60)
        if r is None:
            print(f"  warn: page at retstart={retstart} failed; stopping pagination")
            break
        batch = r.json().get("esearchresult", {}).get("idlist") or []
        if not batch:
            break
        ids.extend(batch)
        retstart = len(ids)
        print(f"  fetched so far: {len(ids):,}")

    if limit > 0:
        ids = ids[:limit]
    return ids


def load_metadata(version_id: str) -> dict[str, Any] | None:
    """Load metadata/PMC{id}.{ver}.json"""
    url = f"{S3_HTTP}/metadata/{version_id}.json"
    r = http_get(url, timeout=20)
    if r is None:
        return None
    try:
        return r.json()
    except Exception:
        return None


def resolve_version_id(pmcid: str) -> tuple[str, dict[str, Any]] | None:
    """
    pmcid may be 'PMC123' or '123'. Try version 1..3.
    Returns (version_id, meta).
    """
    pmcid = pmcid.strip()
    if not pmcid.upper().startswith("PMC"):
        pmcid = f"PMC{pmcid}"
    base = pmcid.upper() if pmcid.upper().startswith("PMC") else f"PMC{pmcid}"
    # Keep original casing style PMC...
    if not pmcid.startswith("PMC"):
        pmcid = f"PMC{pmcid}"

    for ver in (1, 2, 3):
        version_id = f"{pmcid}.{ver}"
        meta = load_metadata(version_id)
        if meta:
            return version_id, meta
    return None


def is_commercial_meta(meta: dict[str, Any]) -> bool:
    code = (meta.get("license_code") or "").strip().upper()
    # Normalise spacing
    code_norm = " ".join(code.replace("_", " ").split())
    allowed = {c.upper() for c in COMMERCIAL_LICENSE_CODES}
    if code_norm in allowed:
        return True
    # Prefix match e.g. "CC BY 3.0"
    if code_norm.startswith("CC0"):
        return True
    if code_norm.startswith("CC BY-SA") or code_norm.startswith("CC BY SA"):
        return True
    if code_norm.startswith("CC BY-ND") or code_norm.startswith("CC BY ND"):
        return True
    if code_norm.startswith("CC BY") and "NC" not in code_norm:
        return True
    return False


def s3_to_http(s3_url: str) -> str:
    path = s3_url.replace("s3://pmc-oa-opendata/", "").split("?")[0]
    return f"{S3_HTTP}/{path}"


def download_to_file(url: str, out_path: Path, dry_run: bool = False) -> bool:
    if dry_run:
        return True
    if out_path.exists() and out_path.stat().st_size > 0:
        return True
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    r = http_get(url, timeout=120, stream=True)
    if r is None:
        return False
    try:
        with tmp.open("wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
        tmp.replace(out_path)
        return True
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        return False


def process_one(
    pmcid: str,
    output_dir: Path,
    formats: list[str],
    dry_run: bool,
) -> tuple[str, bool, str | None]:
    resolved = resolve_version_id(pmcid)
    if not resolved:
        return pmcid, False, "metadata not found"
    version_id, meta = resolved

    if not is_commercial_meta(meta):
        return pmcid, False, f"non-commercial license_code={meta.get('license_code')!r}"

    # Persist metadata
    meta_dir = output_dir / "metadata"
    meta_path = meta_dir / f"{version_id}.json"
    if not dry_run:
        meta_dir.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    fmt_map = {
        "xml": "xml_url",
        "txt": "text_url",
        "text": "text_url",
        "pdf": "pdf_url",
    }

    for fmt in formats:
        key = fmt_map.get(fmt)
        if not key:
            continue
        s3_url = meta.get(key)
        if not s3_url:
            continue
        ext = "txt" if fmt in ("txt", "text") else fmt
        out_file = output_dir / ("txt" if fmt == "text" else fmt) / f"{version_id}.{ext}"
        ok = download_to_file(s3_to_http(s3_url), out_file, dry_run=dry_run)
        if not ok:
            return pmcid, False, f"failed download {fmt}"

    return pmcid, True, None


def collect_ids_from_filelist(path: Path, limit: int) -> list[str]:
    rows = parse_filelist(path)
    ids: list[str] = []
    for row in rows:
        acc = accession_from_filelist_row(row)
        if not acc:
            continue
        ids.append(acc)
        if limit > 0 and len(ids) >= limit:
            break
    return ids


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PMC Commercial OA downloader (filelist or ESearch + metadata verify)"
    )
    parser.add_argument(
        "--output_dir",
        type=Path,
        default=Path("./01_raw/pmc/oa_comm"),
        help="Output root",
    )
    parser.add_argument(
        "--formats",
        nargs="+",
        default=["xml"],
        help="Formats: xml and/or txt (pdf optional)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Max articles (0 = all available via chosen ID source)",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=6,
        help="Parallel download workers",
    )
    parser.add_argument(
        "--dry_run",
        action="store_true",
        help="Resolve IDs / metadata only; do not write article files",
    )
    parser.add_argument(
        "--api_key",
        type=str,
        default=get_settings().ncbi_api_key,
        help="NCBI API key (or env NCBI_API_KEY)",
    )
    parser.add_argument(
        "--filelist",
        type=Path,
        default=None,
        help="Use an existing local filelist CSV/TSV instead of downloading/ESearch",
    )
    args = parser.parse_args()

    formats = []
    for f in args.formats:
        formats.extend(f.split())
    formats = [f.lower() for f in formats]

    out: Path = args.output_dir
    out.mkdir(parents=True, exist_ok=True)

    ids: list[str] = []

    # 1) Explicit local filelist
    if args.filelist and args.filelist.is_file():
        print(f"Using local filelist: {args.filelist}")
        ids = collect_ids_from_filelist(args.filelist, args.limit)
    else:
        # 2) Try bucket filelist
        fl = try_download_filelist(out / "filelists")
        if fl is not None:
            ids = collect_ids_from_filelist(fl, args.limit)
            print(f"IDs from filelist: {len(ids):,}")
        else:
            # 3) ESearch commercial-only
            ids = esearch_commercial_ids(args.limit, api_key=args.api_key)
            print(f"IDs from ESearch: {len(ids):,}")

    if not ids:
        print("No commercial IDs to download.")
        sys.exit(1)

    print(
        f"Starting {'dry-run ' if args.dry_run else ''}download of "
        f"{len(ids)} articles, formats={formats}, threads={args.threads}"
    )

    ok_n = 0
    failures: list[tuple[str, str]] = []

    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        futs = {
            ex.submit(process_one, pmcid, out, formats, args.dry_run): pmcid
            for pmcid in ids
        }
        for fut in tqdm(as_completed(futs), total=len(futs), desc="PMC commercial"):
            pmcid = futs[fut]
            try:
                pid, success, err = fut.result()
                if success:
                    ok_n += 1
                else:
                    failures.append((pid, err or "unknown"))
            except Exception as e:  # noqa: BLE001
                failures.append((pmcid, str(e)))

    print(f"\nDone: {ok_n} ok, {len(failures)} failed/skipped")
    if failures:
        print("First failures:")
        for pid, err in failures[:15]:
            print(f"  {pid}: {err}")

    meta_summary = out / "sync_meta.txt"
    meta_summary.write_text(
        "\n".join(
            [
                "subset=oa_comm_commercial",
                f"formats={' '.join(formats)}",
                f"source={S3_HTTP}/",
                "filter=commercial_license_only",
                f"requested_ids={len(ids)}",
                f"succeeded={ok_n}",
                f"failed_or_skipped={len(failures)}",
                f"synced_utc={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
                "",
            ]
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
