#!/usr/bin/env python3
"""
Episteme – PMC Commercial OA Downloader (post Aug 2026 layout)

Source of the article list, in order:
  1. --filelist (a local CSV/TSV), when given
  2. the bucket's daily S3 inventory of metadata JSON objects (the bucket
     README, section 5.2): one row per article version, so every version of
     every PMCID is known without listing ~8 million prefixes
  3. NCBI ESearch with commercial licence filters, only when the inventory
     cannot be read; ESearch returns at most 9,999 ids, so it refuses a larger
     request instead of failing part way through
  Always verify license_code from metadata JSON before saving.

Live object layout (since the 2026-08-24 cutover, which removed the oa_comm
filelists, the FTP oa_bulk packages and the baseline packages):
  https://pmc-oa-opendata.s3.amazonaws.com/metadata/PMC{id}.{ver}.json
  https://pmc-oa-opendata.s3.amazonaws.com/PMC{id}.{ver}/PMC{id}.{ver}.xml
  https://pmc-oa-opendata.s3.amazonaws.com/inventory-reports/pmc-oa-opendata/metadata/
      {date}/manifest.json -> its files[].key -> data/{uuid}.csv.gz
  (xml_url / text_url inside metadata JSON)

One version per PMCID: the highest-numbered commercial version. Version numbers
reflect processing order, so this is the latest one PMC processed. A PMCID whose
versions are all non-commercial is recorded in filelists/noncommercial.txt, so a
restart does not fetch its metadata again (delete the file to re-check them).

Commercial license_code values kept:
  CC0, CC BY, CC BY-SA, CC BY-ND
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from tqdm import tqdm

from episteme.config import get_settings

S3_HTTP = "https://pmc-oa-opendata.s3.amazonaws.com"

INVENTORY_PREFIX = "inventory-reports/pmc-oa-opendata/metadata/"
_INVENTORY_DATE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}Z)/$")
_INVENTORY_KEY = re.compile(r"^metadata/(PMC\d+)\.(\d+)\.json$")
# One inventory is ~15 csv.gz files (~250 MB); one lost file would drop the run to
# the ESearch fallback, so each file gets more attempts than an article does.
INVENTORY_RETRIES = 8

# ESearch never returns an id past position 9,999.
ESEARCH_MAX_IDS = 9999

NONCOMMERCIAL_LEDGER = "noncommercial.txt"
NONCOMMERCIAL_REASON = "non-commercial"

# Futures in flight at once; main() submits the id list in batches of this size so
# ~8 million ids never become ~8 million pending futures.
SUBMIT_BATCH = 2000

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


def latest_inventory_date(list_xml: str) -> str | None:
    """The newest ``YYYY-MM-DDTHH-MMZ`` folder in a ListObjectsV2 reply for
    INVENTORY_PREFIX with ``delimiter=/`` (the ``data/`` and ``hive/`` folders do
    not match the date pattern)."""
    prefixes = re.findall(r"<Prefix>([^<]+)</Prefix>", list_xml)
    dates = [m.group(1) for p in prefixes if (m := _INVENTORY_DATE.search(p))]
    return max(dates) if dates else None


def inventory_versions(rows: Iterable[list[str]]) -> dict[str, list[int]]:
    """Map PMCID -> its version numbers, highest first, from inventory CSV rows
    (``bucket, key, last_modified, etag``; ``key`` is ``metadata/PMC{id}.{ver}.json``).
    Rows with any other key are ignored."""
    found: dict[str, set[int]] = {}
    for row in rows:
        if len(row) < 2:
            continue
        m = _INVENTORY_KEY.match(row[1].strip())
        if m:
            found.setdefault(m.group(1), set()).add(int(m.group(2)))
    return {pmcid: sorted(vers, reverse=True) for pmcid, vers in found.items()}


def fetch_inventory(dest_dir: Path) -> tuple[str, dict[str, list[int]]] | None:
    """Download the newest bucket inventory into ``dest_dir/<date>/`` (a file
    already there is reused) and return ``(date, versions)``, or None when the
    inventory cannot be read."""
    query = urlencode({"list-type": "2", "prefix": INVENTORY_PREFIX, "delimiter": "/"})
    listing = http_get(f"{S3_HTTP}/?{query}", timeout=60)
    date = latest_inventory_date(listing.text) if listing is not None else None
    if date is None:
        print("No bucket inventory found.", file=sys.stderr)
        return None
    manifest_r = http_get(f"{S3_HTTP}/{INVENTORY_PREFIX}{date}/manifest.json", timeout=60)
    if manifest_r is None:
        print(f"Inventory {date}: manifest.json not readable.", file=sys.stderr)
        return None
    keys = [f["key"] for f in manifest_r.json().get("files", []) if f.get("key")]
    if not keys:
        print(f"Inventory {date}: manifest lists no files.", file=sys.stderr)
        return None
    inv_dir = dest_dir / date
    versions: dict[str, list[int]] = {}
    for key in keys:
        local = inv_dir / key.rsplit("/", 1)[-1]
        if not download_to_file(f"{S3_HTTP}/{key}", local, retries=INVENTORY_RETRIES):
            print(f"Inventory {date}: cannot download {key}.", file=sys.stderr)
            return None
        with gzip.open(local, "rt", encoding="utf-8", newline="") as fh:
            for pmcid, vers in inventory_versions(csv.reader(fh)).items():
                versions[pmcid] = sorted(set(versions.get(pmcid, [])) | set(vers), reverse=True)
    print(f"Inventory {date}: {len(versions):,} PMCIDs from {len(keys)} file(s)")
    return date, versions


def _pmc_number(pmcid: str) -> int:
    digits = pmcid[3:]
    return int(digits) if digits.isdigit() else 0


def parse_filelist(path: Path) -> list[dict[str, str]]:
    """Parse CSV or TSV filelist into row dicts."""
    text = path.read_text(encoding="utf-8", errors="replace")
    dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t")
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    rows: list[dict[str, str]] = []
    for row in reader:
        # Normalise keys
        norm = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
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

    ESearch returns no id past position ESEARCH_MAX_IDS, so a request for more
    (``limit`` 0 or above the cap, with more matches than the cap) exits with an
    error before any download starts.
    """
    base = "https://eutils.ncbi.nlm.nih.gov/eutils/esearch.fcgi"
    page_size = ESEARCH_MAX_IDS
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
    print(f"  total matching (NCBI count): {total:,}")
    target = min(limit, total) if limit > 0 else total
    if target > ESEARCH_MAX_IDS:
        sys.exit(
            f"ERROR: ESearch returns at most {ESEARCH_MAX_IDS:,} ids and {target:,} were "
            "requested; the bucket inventory is the source for a full download "
            "(see the module docstring)"
        )

    webenv = data.get("webenv")
    query_key = data.get("querykey")
    ids.extend(data.get("idlist") or [])
    print(f"  fetched so far: {len(ids):,}")

    retstart = len(ids)
    while retstart < target and webenv and query_key:
        time.sleep(0.34 if api_key else 0.4)  # be polite to NCBI
        params = {
            "db": "pmc",
            "query_key": query_key,
            "WebEnv": webenv,
            "retmode": "json",
            "retstart": retstart,
            "retmax": target - retstart,
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


def _normalise_pmcid(pmcid: str) -> str:
    pmcid = pmcid.strip()
    if not pmcid.upper().startswith("PMC"):
        pmcid = f"PMC{pmcid}"
    # Keep original casing style PMC...
    if not pmcid.startswith("PMC"):
        pmcid = f"PMC{pmcid}"
    return pmcid


def resolve_version_id(pmcid: str) -> tuple[str, dict[str, Any]] | None:
    """
    pmcid may be 'PMC123' or '123'. Try version 1..3.
    Returns (version_id, meta).
    """
    pmcid = _normalise_pmcid(pmcid)
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


def download_to_file(url: str, out_path: Path, dry_run: bool = False, retries: int = 3) -> bool:
    if dry_run:
        return True
    if out_path.exists() and out_path.stat().st_size > 0:
        return True
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    r = http_get(url, timeout=120, stream=True, retries=retries)
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


_FMT_KEY = {"xml": "xml_url", "txt": "text_url", "text": "text_url", "pdf": "pdf_url"}


def _out_file(output_dir: Path, version_id: str, fmt: str) -> Path:
    ext = "txt" if fmt in ("txt", "text") else fmt
    return output_dir / ("txt" if fmt == "text" else fmt) / f"{version_id}.{ext}"


def _already_downloaded(output_dir: Path, version_ids: list[str], formats: list[str]) -> bool:
    """True when one of ``version_ids`` has its metadata JSON and every requested
    format on disk, so a restart can skip it without any request."""
    wanted = [f for f in formats if f in _FMT_KEY]
    for vid in version_ids:
        if not (output_dir / "metadata" / f"{vid}.json").is_file():
            continue
        if all(_out_file(output_dir, vid, f).is_file() for f in wanted):
            return True
    return False


def process_one(
    pmcid: str,
    output_dir: Path,
    formats: list[str],
    dry_run: bool,
    versions: list[int] | None = None,
) -> tuple[str, bool, str | None]:
    """Download one article. With ``versions`` (from the inventory, highest
    first) the highest commercial version is kept; without them the first of
    versions 1..3 that exists is used, as before."""
    if versions:
        pmcid = _normalise_pmcid(pmcid)
        candidates = [f"{pmcid}.{v}" for v in versions]
        if _already_downloaded(output_dir, candidates, formats):
            return pmcid, True, None
        resolved = None
        seen_codes: list[Any] = []
        for vid in candidates:
            meta = load_metadata(vid)
            if meta is None:
                continue
            if is_commercial_meta(meta):
                resolved = (vid, meta)
                break
            seen_codes.append(meta.get("license_code"))
        if resolved is None:
            if seen_codes:
                return pmcid, False, f"{NONCOMMERCIAL_REASON} license_code={seen_codes[0]!r}"
            return pmcid, False, "metadata not found"
    else:
        resolved = resolve_version_id(pmcid)
        if not resolved:
            return pmcid, False, "metadata not found"
    version_id, meta = resolved

    if not is_commercial_meta(meta):
        return pmcid, False, f"{NONCOMMERCIAL_REASON} license_code={meta.get('license_code')!r}"

    # Persist metadata
    meta_dir = output_dir / "metadata"
    meta_path = meta_dir / f"{version_id}.json"
    if not dry_run:
        meta_dir.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    for fmt in formats:
        key = _FMT_KEY.get(fmt)
        if not key:
            continue
        s3_url = meta.get(key)
        if not s3_url:
            continue
        out_file = _out_file(output_dir, version_id, fmt)
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


def read_noncommercial(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def _batches(items: list[Any], size: int) -> Iterator[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def build_parser() -> argparse.ArgumentParser:
    settings = get_settings()

    parser = argparse.ArgumentParser(
        description="PMC Commercial OA downloader (bucket inventory or ESearch + metadata verify)"
    )
    parser.add_argument(
        "--output_dir",
        type=Path,
        default=settings.raw_root / "pmc" / "oa_comm",
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
        default=16,
        help="Parallel download workers",
    )
    parser.add_argument(
        "--dry-run",
        "--dry_run",
        dest="dry_run",
        action="store_true",
        help="Print what would be downloaded and exit without resolving IDs or writing files",
    )
    parser.add_argument(
        "--api_key",
        type=str,
        default=settings.ncbi_api_key,
        help="NCBI API key (or env NCBI_API_KEY)",
    )
    parser.add_argument(
        "--filelist",
        type=Path,
        default=None,
        help="Use an existing local filelist CSV/TSV instead of the inventory/ESearch",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    formats = []
    for f in args.formats:
        formats.extend(f.split())
    formats = [f.lower() for f in formats]

    if args.dry_run:
        print(
            f"dry-run: would download PMC commercial OA from {S3_HTTP}/ "
            f"(filter=commercial_license_only, limit={args.limit}, formats={formats}); "
            "resolving nothing, writing nothing"
        )
        return

    out: Path = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    filelists = out / "filelists"
    filelists.mkdir(parents=True, exist_ok=True)

    ids: list[str] = []
    versions: dict[str, list[int]] = {}
    id_source = ""

    # 1) Explicit local filelist
    if args.filelist and args.filelist.is_file():
        print(f"Using local filelist: {args.filelist}")
        ids = collect_ids_from_filelist(args.filelist, args.limit)
        id_source = f"filelist:{args.filelist}"
    else:
        # 2) Bucket inventory
        inv = fetch_inventory(filelists / "inventory")
        if inv is not None:
            inv_date, versions = inv
            ids = sorted(versions, key=_pmc_number)
            if args.limit > 0:
                ids = ids[: args.limit]
            id_source = f"inventory:{inv_date}"
        else:
            # 3) ESearch commercial-only
            ids = esearch_commercial_ids(args.limit, api_key=args.api_key)
            id_source = "esearch"
    print(f"IDs from {id_source or 'nowhere'}: {len(ids):,}")

    if not ids:
        print("No commercial IDs to download.")
        sys.exit(1)

    ledger = filelists / NONCOMMERCIAL_LEDGER
    known_nc = read_noncommercial(ledger)
    todo = [i for i in ids if i not in known_nc]
    print(
        f"Starting download of {len(todo):,} articles ({len(ids) - len(todo):,} known "
        f"non-commercial skipped), formats={formats}, threads={args.threads}"
    )

    ok_n = 0
    nc_n = 0
    fail_n = 0
    reasons: Counter[str] = Counter()
    samples: list[tuple[str, str]] = []

    with (
        ThreadPoolExecutor(max_workers=args.threads) as ex,
        ledger.open("a", encoding="utf-8") as ledger_fh,
        tqdm(total=len(todo), desc="PMC commercial") as bar,
    ):
        for batch in _batches(todo, SUBMIT_BATCH):
            futs = [
                (
                    pmcid,
                    ex.submit(process_one, pmcid, out, formats, False, versions.get(pmcid)),
                )
                for pmcid in batch
            ]
            for pmcid, fut in futs:
                try:
                    pid, success, err = fut.result()
                except Exception as e:  # noqa: BLE001
                    pid, success, err = pmcid, False, str(e)
                bar.update(1)
                if success:
                    ok_n += 1
                    continue
                err = err or "unknown"
                if err.startswith(NONCOMMERCIAL_REASON):
                    nc_n += 1
                    ledger_fh.write(f"{pmcid}\n")
                else:
                    fail_n += 1
                    reasons[err.split(" license_code")[0]] += 1
                    if len(samples) < 15:
                        samples.append((pid, err))
            ledger_fh.flush()

    print(f"\nDone: {ok_n:,} ok, {nc_n:,} non-commercial, {fail_n:,} failed")
    if samples:
        print("Failure reasons:", dict(reasons))
        print("First failures:")
        for pid, err in samples:
            print(f"  {pid}: {err}")

    meta_summary = out / "sync_meta.txt"
    meta_summary.write_text(
        "\n".join(
            [
                "subset=oa_comm_commercial",
                f"formats={' '.join(formats)}",
                f"source={S3_HTTP}/",
                f"id_source={id_source}",
                "filter=commercial_license_only",
                f"requested_ids={len(ids)}",
                f"succeeded={ok_n}",
                f"noncommercial={nc_n + len(ids) - len(todo)}",
                f"failed={fail_n}",
                f"synced_utc={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    # A failed article is retried by the next run (nothing marks it done). A
    # version missing from the bucket (the inventory lags it by up to a day) is
    # reported but does not fail the run.
    if fail_n - reasons["metadata not found"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
