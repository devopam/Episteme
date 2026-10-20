#!/usr/bin/env python3
"""
Episteme – PMC Commercial OA Downloader (post Aug 2026 layout)

Source of the article list, in order:
  1. --filelist (a local CSV/TSV), when given
  2. the bucket's daily S3 inventory of metadata JSON objects (the bucket
     README, section 5.2): one row per article version, so every version of
     every PMCID is known without listing ~8 million prefixes. When the newest
     inventory cannot be read (not yet published, or a file is damaged), the
     next older one is used.
  3. NCBI ESearch with commercial licence filters, only when no inventory can
     be read; ESearch returns at most 9,999 ids, so it refuses a larger request
     instead of failing part way through
  Always verify license_code from metadata JSON before saving.

Live object layout (since the 2026-08-24 cutover, which removed the oa_comm
filelists, the FTP oa_bulk packages and the baseline packages):
  https://pmc-oa-opendata.s3.amazonaws.com/metadata/PMC{id}.{ver}.json
  https://pmc-oa-opendata.s3.amazonaws.com/PMC{id}.{ver}/PMC{id}.{ver}.xml
  https://pmc-oa-opendata.s3.amazonaws.com/inventory-reports/pmc-oa-opendata/metadata/
      {date}/manifest.json -> its files[].key -> data/{uuid}.csv.gz
  (xml_url / text_url inside metadata JSON)

One version per PMCID: the highest-numbered commercial version. Version numbers
reflect processing order, so this is the latest one PMC processed. Without the
inventory (paths 1 and 3) the versions of a PMCID come from a ListObjectsV2 call
on its ``PMC{id}.`` prefix.

A PMCID whose versions are all non-commercial is recorded in
filelists/noncommercial.txt as ``PMC{id}.{highest version}``; a re-run skips it
while the inventory shows the same highest version, and checks it again when a
newer version appears. Delete the file to re-check every PMCID. A metadata
request that fails (anything but a 404) counts as a failure: the PMCID is not
recorded, no older version is chosen in its place, and the run exits 1.

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
import threading
import time
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from tqdm import tqdm

from episteme.config import get_settings
from episteme.ops import lock as ops_lock
from episteme.ops import profile as ops_profile
from episteme.ops import status as ops_status

S3_HTTP = "https://pmc-oa-opendata.s3.amazonaws.com"

INVENTORY_PREFIX = "inventory-reports/pmc-oa-opendata/metadata/"
_INVENTORY_DATE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}Z)/$")
_INVENTORY_KEY = re.compile(r"^metadata/PMC(\d+)\.(\d+)\.json$")
# One inventory is ~15 csv.gz files (~250 MB); one lost file would drop the run to
# the ESearch fallback, so each file gets more attempts than an article does.
INVENTORY_RETRIES = 8
# The newest inventories tried, newest first, before the ESearch fallback.
INVENTORY_DATES_TRIED = 3

# ESearch never returns an id past position 9,999.
ESEARCH_MAX_IDS = 9999

NONCOMMERCIAL_LEDGER = "noncommercial.txt"
NONCOMMERCIAL_REASON = "non-commercial"
REQUEST_FAILED = "request failed"
MISSING = "metadata not found"

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


class FetchError(Exception):
    """A request that failed after its retries (not a 404), or a reply that does
    not parse."""


_LOCAL = threading.local()


def _session() -> requests.Session:
    """One requests.Session per thread. A bare requests.get opens a new TLS
    connection for every call; to the us-east-1 bucket from far away that cost
    about a second per request and held the full run to ~3 articles/s
    (2026-10-18). A Session keeps the connection open between calls."""
    s = getattr(_LOCAL, "session", None)
    if s is None:
        s = requests.Session()
        s.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=2))
        _LOCAL.session = s
    return s


def http_get_checked(
    url: str,
    *,
    timeout: int = 60,
    stream: bool = False,
    retries: int = 3,
) -> requests.Response | None:
    """GET with retries: the response on 200, None on 404, FetchError otherwise."""
    last = "no attempt"
    for attempt in range(retries):
        try:
            r = _session().get(url, timeout=timeout, stream=stream)
            if r.status_code == 200:
                return r
            r.close()  # give the connection back to the pool
            if r.status_code == 404:
                return None
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(1.5 * (attempt + 1))
    raise FetchError(f"GET {url}: {last}")


def http_get(
    url: str,
    *,
    timeout: int = 60,
    stream: bool = False,
    retries: int = 3,
) -> requests.Response | None:
    """Like http_get_checked, but a failure is a warning and returns None."""
    try:
        return http_get_checked(url, timeout=timeout, stream=stream, retries=retries)
    except FetchError as e:
        print(f"  warn: {e}", file=sys.stderr)
        return None


def inventory_dates(list_xml: str) -> list[str]:
    """The ``YYYY-MM-DDTHH-MMZ`` folders in a ListObjectsV2 reply for
    INVENTORY_PREFIX with ``delimiter=/``, newest first (the ``data/`` and
    ``hive/`` folders do not match the date pattern)."""
    prefixes = re.findall(r"<Prefix>([^<]+)</Prefix>", list_xml)
    return sorted({m.group(1) for p in prefixes if (m := _INVENTORY_DATE.search(p))}, reverse=True)


def latest_inventory_date(list_xml: str) -> str | None:
    dates = inventory_dates(list_xml)
    return dates[0] if dates else None


# The full inventory holds ~9.3 million PMCIDs and ~98% have one version, so it is
# kept compact: PMC number -> its one version as an int, or a tuple (highest first)
# when there are several. A dict of str -> set took 3.4 GiB at peak; this form needs
# about a third of that.
Versions = int | tuple[int, ...]


def _versions_of(value: Versions) -> tuple[int, ...]:
    return (value,) if isinstance(value, int) else value


def _add_inventory_rows(found: dict[int, Versions], rows: Iterable[list[str]]) -> None:
    for row in rows:
        if len(row) < 2:
            continue
        m = _INVENTORY_KEY.match(row[1].strip())
        if not m:
            continue
        n, v = int(m.group(1)), int(m.group(2))
        cur = found.get(n)
        if cur is None:
            found[n] = v
        elif v not in _versions_of(cur):
            found[n] = tuple(sorted({*_versions_of(cur), v}, reverse=True))


def inventory_versions(rows: Iterable[list[str]]) -> dict[str, tuple[int, ...]]:
    """Map PMCID -> its version numbers, highest first, from inventory CSV rows
    (``bucket, key, last_modified, etag``; ``key`` is ``metadata/PMC{id}.{ver}.json``).
    Rows with any other key are ignored."""
    found: dict[int, Versions] = {}
    _add_inventory_rows(found, rows)
    return {f"PMC{n}": _versions_of(v) for n, v in found.items()}


def _read_inventory(dest_dir: Path, date: str) -> dict[int, Versions] | None:
    """Versions from one inventory date, or None when its manifest or a file
    cannot be read. A damaged local file is deleted so the next run downloads it
    again."""
    local: Path | None = None
    try:
        manifest_r = http_get(f"{S3_HTTP}/{INVENTORY_PREFIX}{date}/manifest.json", timeout=60)
        if manifest_r is None:
            print(f"Inventory {date}: manifest.json not readable.", file=sys.stderr)
            return None
        keys = [f["key"] for f in manifest_r.json().get("files", []) if f.get("key")]
        if not keys:
            print(f"Inventory {date}: manifest lists no files.", file=sys.stderr)
            return None
        found: dict[int, Versions] = {}
        for key in keys:
            local = dest_dir / date / key.rsplit("/", 1)[-1]
            if not download_to_file(f"{S3_HTTP}/{key}", local, retries=INVENTORY_RETRIES):
                print(f"Inventory {date}: cannot download {key}.", file=sys.stderr)
                return None
            with gzip.open(local, "rt", encoding="utf-8", newline="") as fh:
                _add_inventory_rows(found, csv.reader(fh))
        print(f"Inventory {date}: {len(found):,} PMCIDs from {len(keys)} file(s)")
        return found
    except (ValueError, KeyError, TypeError, OSError, EOFError, csv.Error) as e:
        print(f"Inventory {date}: not readable ({type(e).__name__}: {e}).", file=sys.stderr)
        if local is not None:
            local.unlink(missing_ok=True)
        return None


def fetch_inventory(dest_dir: Path) -> tuple[str, dict[int, Versions]] | None:
    """Read the newest readable bucket inventory (up to INVENTORY_DATES_TRIED
    dates, newest first) into ``dest_dir/<date>/`` (a file already there is
    reused) and return ``(date, versions)``: PMC number -> Versions."""
    query = urlencode({"list-type": "2", "prefix": INVENTORY_PREFIX, "delimiter": "/"})
    listing = http_get(f"{S3_HTTP}/?{query}", timeout=60)
    dates = inventory_dates(listing.text) if listing is not None else []
    if not dates:
        print("No bucket inventory found.", file=sys.stderr)
        return None
    for date in dates[:INVENTORY_DATES_TRIED]:
        versions = _read_inventory(dest_dir, date)
        if versions is not None:
            return date, versions
    return None


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
    """metadata/PMC{id}.{ver}.json, or None on a 404. A failed request or a
    reply that is not a JSON object raises FetchError."""
    r = http_get_checked(f"{S3_HTTP}/metadata/{version_id}.json", timeout=20)
    if r is None:
        return None
    try:
        meta = r.json()
    except ValueError as e:
        raise FetchError(f"metadata {version_id}: not JSON ({e})") from e
    if not isinstance(meta, dict):
        raise FetchError(f"metadata {version_id}: not a JSON object")
    return meta


def list_versions(pmcid: str) -> tuple[int, ...]:
    """The versions of ``pmcid`` in the bucket, highest first, from a
    ListObjectsV2 call on its ``PMC{id}.`` prefix (the README's section 5.1 step
    5). Used when the inventory is not the id source."""
    query = urlencode({"list-type": "2", "prefix": f"{pmcid}.", "delimiter": "/"})
    r = http_get_checked(f"{S3_HTTP}/?{query}", timeout=30)
    if r is None:
        return ()
    found = re.findall(rf"<Prefix>{re.escape(pmcid)}\.(\d+)/</Prefix>", r.text)
    return tuple(sorted({int(v) for v in found}, reverse=True))


def _normalise_pmcid(pmcid: str) -> str:
    pmcid = pmcid.strip()
    if not pmcid.upper().startswith("PMC"):
        pmcid = f"PMC{pmcid}"
    # Keep original casing style PMC...
    if not pmcid.startswith("PMC"):
        pmcid = f"PMC{pmcid}"
    return pmcid


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
    """Download ``url`` to ``out_path`` through a temp file. A non-empty file
    already there counts as done; an empty reply is a failure (nothing is kept)."""
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
        if tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            return False
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


def _nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _already_downloaded(output_dir: Path, version_id: str, formats: list[str]) -> bool:
    """True when ``version_id`` has its metadata JSON and every requested format
    on disk, none of them empty."""
    if not _nonempty(output_dir / "metadata" / f"{version_id}.json"):
        return False
    return all(_nonempty(_out_file(output_dir, version_id, f)) for f in formats if f in _FMT_KEY)


def process_one(
    pmcid: str,
    output_dir: Path,
    formats: list[str],
    dry_run: bool,
    versions: Iterable[int] | None = None,
) -> tuple[str, bool, str | None]:
    """Download one article: the highest commercial version of ``pmcid``.

    ``versions`` (highest first) come from the inventory; without them they are
    listed from the bucket. Versions are checked from the highest down: one
    already on disk ends the check with no request, and a non-commercial one is
    passed over. A failed request ends it with a REQUEST_FAILED error, so no older
    version is chosen in place of a version that could not be read.
    """
    pmcid = _normalise_pmcid(pmcid)
    try:
        vers = tuple(versions) if versions else list_versions(pmcid)
        if not vers:
            return pmcid, False, MISSING
        candidates = [f"{pmcid}.{v}" for v in vers]
        resolved = None
        seen_codes: list[Any] = []
        for vid in candidates:
            if _already_downloaded(output_dir, vid, formats):
                return pmcid, True, None
            meta = load_metadata(vid)
            if meta is None:
                continue
            if is_commercial_meta(meta):
                resolved = (vid, meta)
                break
            seen_codes.append(meta.get("license_code"))
    except FetchError as e:
        return pmcid, False, f"{REQUEST_FAILED}: {e}"
    if resolved is None:
        if seen_codes:
            return (
                pmcid,
                False,
                f"{NONCOMMERCIAL_REASON} license_code={seen_codes[0]!r} highest={candidates[0]}",
            )
        return pmcid, False, MISSING
    version_id, meta = resolved

    # Persist metadata
    meta_dir = output_dir / "metadata"
    meta_path = meta_dir / f"{version_id}.json"
    if not dry_run:
        meta_dir.mkdir(parents=True, exist_ok=True)

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

    # Written last: the metadata JSON marks the version complete for a re-run.
    if not dry_run:
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
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
    """Ledger lines: ``PMC{id}.{highest version}`` (a bare ``PMC{id}`` from an
    older run is kept as it is)."""
    if not path.is_file():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def _one_line(text: str) -> str:
    """One line for failed_files.txt: a tab or newline would split the record."""
    return text.replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _ledger_entry(pmcid: str, err: str) -> str:
    return err.rsplit("highest=", 1)[1] if "highest=" in err else pmcid


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
        help="Stop after this many articles are downloaded (0 = all)",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=None,
        help=(
            "Parallel download workers, each with its own open connection "
            "(default: the runtime profile (48))"
        ),
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


def resolve_threads(args: argparse.Namespace) -> int:
    """The --threads value, or the runtime profile's when --threads is not given."""
    if args.threads is None:
        args.threads = ops_profile.get_profile().pmc_threads
    return args.threads


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

    resolve_threads(args)  # after the dry run: the profile runs disk detection

    out: Path = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    filelists = out / "filelists"
    filelists.mkdir(parents=True, exist_ok=True)

    ids: list[str] = []
    inventory: dict[int, Versions] = {}
    id_source = ""

    # 1) Explicit local filelist
    if args.filelist and args.filelist.is_file():
        print(f"Using local filelist: {args.filelist}")
        ids = collect_ids_from_filelist(args.filelist, args.limit)
        id_source = f"filelist:{args.filelist}"
    else:
        # 2) Bucket inventory; --limit counts downloads, so every id is kept
        inv = fetch_inventory(filelists / "inventory")
        if inv is not None:
            inv_date, inventory = inv
            id_source = f"inventory:{inv_date}"
        else:
            # 3) ESearch commercial-only
            ids = esearch_commercial_ids(args.limit, api_key=args.api_key)
            id_source = "esearch"
    n_ids = len(inventory) or len(ids)
    print(f"IDs from {id_source or 'nowhere'}: {n_ids:,}")

    if not n_ids:
        print("No commercial IDs to download.")
        sys.exit(1)

    # With the inventory a PMCID is skipped only while its highest version is the
    # one recorded in the ledger; without it, any record of the PMCID skips it.
    # Inventory items stay PMC numbers (ints) until they are submitted.
    ledger_path = filelists / NONCOMMERCIAL_LEDGER
    ledger = read_noncommercial(ledger_path)
    todo: list[int] | list[str]
    if inventory:
        todo = [
            n for n in sorted(inventory) if f"PMC{n}.{_versions_of(inventory[n])[0]}" not in ledger
        ]
    else:
        ledger_ids = {e.split(".", 1)[0] for e in ledger}
        todo = [i for i in ids if _normalise_pmcid(i) not in ledger_ids]
    skipped_nc = n_ids - len(todo)
    del ledger
    print(
        f"Starting download of {len(todo):,} articles ({skipped_nc:,} known "
        f"non-commercial skipped), formats={formats}, threads={args.threads}"
        + (f", stop after {args.limit:,} downloads" if args.limit > 0 else "")
    )

    ok_n = 0
    nc_n = 0
    fail_n = 0
    reasons: Counter[str] = Counter()
    samples: list[tuple[str, str]] = []

    settings = get_settings()
    if settings.run_dir:
        settings.run_dir.mkdir(parents=True, exist_ok=True)
        failed_path: Path | None = settings.run_dir / "failed_files.txt"
    else:
        failed_path = None
    # Under run_pipeline.sh: one progress record per submit batch (the stall check).
    run_status = (
        ops_status.RunStatus(ops_status.run_dir(settings.run_id))
        if settings.run_id
        else ops_status.NullStatus()
    )

    with (
        ThreadPoolExecutor(max_workers=args.threads) as ex,
        failed_path.open("a", encoding="utf-8") if failed_path else nullcontext() as failed_fh,
        ledger_path.open("a", encoding="utf-8") as ledger_fh,
        tqdm(total=args.limit if args.limit > 0 else len(todo), desc="PMC commercial") as bar,
    ):
        start = 0
        while start < len(todo) and not (args.limit > 0 and ok_n >= args.limit):
            size = SUBMIT_BATCH if args.limit <= 0 else max(1, min(SUBMIT_BATCH, args.limit - ok_n))
            batch = todo[start : start + size]
            start += size
            if settings.run_id:  # wait here while a heavy job holds or wants the heavy lock
                ops_lock.pause_point(settings.run_id)
            futs = []
            for item in batch:
                if isinstance(item, int):
                    pmcid, vers = f"PMC{item}", _versions_of(inventory[item])
                else:
                    pmcid, vers = item, None
                futs.append((pmcid, ex.submit(process_one, pmcid, out, formats, False, vers)))
            for pmcid, fut in futs:
                try:
                    pid, success, err = fut.result()
                except Exception as e:  # noqa: BLE001
                    pid, success, err = pmcid, False, f"{REQUEST_FAILED}: {e}"
                if success:
                    ok_n += 1
                    bar.update(1)
                    continue
                if args.limit <= 0:
                    bar.update(1)
                err = err or "unknown"
                if err.startswith(NONCOMMERCIAL_REASON):
                    nc_n += 1
                    ledger_fh.write(f"{_ledger_entry(pid, err)}\n")
                else:
                    fail_n += 1
                    reasons[err.split(":")[0]] += 1
                    if failed_fh:
                        failed_fh.write(f"{pid}\t{_one_line(err)}\n")
                    if len(samples) < 15:
                        samples.append((pid, err))
            ledger_fh.flush()
            if failed_fh:
                failed_fh.flush()
            run_status.progress(units_done=min(start, len(todo)), units_total=len(todo))

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
                f"requested_ids={n_ids}",
                f"succeeded={ok_n}",
                f"noncommercial={nc_n + skipped_nc}",
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
    if fail_n - reasons[MISSING]:
        sys.exit(1)


if __name__ == "__main__":
    main()
