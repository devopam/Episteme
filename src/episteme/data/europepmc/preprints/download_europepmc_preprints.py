#!/usr/bin/env python3
"""Acquisition: europepmc_preprint — EBI's bulk preprint files.

``ftp.ebi.ac.uk/pub/databases/pmc/preprints/`` (``Settings.europepmc_preprint_base``)
holds bulk files ``PPR<a>_PPR<b>.xml.gz`` (one ``<articles>`` root of JATS preprint
records each), ``pprid.txt.gz`` and a privacy notice. This downloader:

1. GETs the folder listing and takes the names that match ``PPR<a>_PPR<b>.xml.gz``
   (no hard-coded list: a new range file is found by the pattern).
2. Skips a file that is already in ``raw_dir`` with the size the server states.
3. Fetches the others into ``raw_dir/<name>.part`` (a stopped download resumes from
   the ``.part`` file with a ``Range`` request) and renames the part on success.

The per-id REST route (one ``fullTextXML`` request per preprint, ``pprid.txt.gz`` and
``.harvest_state``) was removed: the bulk files cover the same records in far fewer
requests.

``--dry-run`` lists what would be fetched and writes nothing. ``--force`` fetches
again (only the files that the cap lets through; the old file stays until the new one is
complete). ``--max-files`` caps the number of files fetched in a run.

No ``os.environ`` reads: the endpoint comes from ``episteme.config.get_settings()``.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

import requests

from episteme.config import get_settings

BULK_NAME_RE = re.compile(r"^PPR(\d+)_PPR\d+\.xml\.gz$")
_HREF_RE = re.compile(r'href="([^"]+)"')
_CHUNK = 1 << 20
_TIMEOUT = (30, 120)


def parse_listing(html: str) -> list[str]:
    """The bulk file names in a folder listing, sorted by first PPR id. A link with a
    path, a query or another name is left out."""
    names: set[str] = set()
    for href in _HREF_RE.findall(html):
        if BULK_NAME_RE.match(href):
            names.add(href)
    return sorted(names, key=lambda n: int(BULK_NAME_RE.match(n).group(1)))  # type: ignore[union-attr]


def _remote_size(session: Any, url: str) -> int | None:
    try:
        resp = session.head(url, allow_redirects=True, timeout=_TIMEOUT)
        resp.raise_for_status()
        return int(resp.headers["Content-Length"])
    except Exception:  # noqa: BLE001 - unknown size: the file is fetched
        return None


def _fetch(session: Any, url: str, dest: Path, *, fresh: bool = False) -> None:
    """Fetch ``url`` into ``dest`` through ``dest.part``; resume from a part file.

    ``dest`` is replaced (``os.replace``) only after the whole file is in the part file, so
    an old good copy survives a failed fetch. A part file that is as big as the file on the
    server or bigger is stale: it is dropped and the file is fetched from the start. A 416
    answer to a ``Range`` request means the same."""
    part = dest.with_name(dest.name + ".part")
    if fresh:
        part.unlink(missing_ok=True)
    have = part.stat().st_size if part.is_file() else 0
    if have:
        remote = _remote_size(session, url)
        if remote is not None and have >= remote:
            part.unlink()
            have = 0
    for attempt in (1, 2):
        headers = {"Range": f"bytes={have}-"} if have else {}
        resp = session.get(url, stream=True, headers=headers, timeout=_TIMEOUT)
        try:
            if resp.status_code == 416 and have and attempt == 1:
                part.unlink(missing_ok=True)  # the part does not fit the file: start over
                have = 0
                continue
            resp.raise_for_status()
            mode = "ab" if have and resp.status_code == 206 else "wb"
            with part.open(mode) as fh:
                for chunk in resp.iter_content(chunk_size=_CHUNK):
                    fh.write(chunk)
        finally:
            resp.close()
        break
    os.replace(part, dest)


def download_bulk(
    raw_dir: Path | str,
    *,
    session: Any = None,
    max_files: int = 0,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, int]:
    """Fetch the bulk files into ``raw_dir``.

    Returns ``{"files", "fetched", "skipped", "errors"}``: ``files`` is the number
    listed, ``skipped`` the number already there with the stated size, ``errors`` the
    fetches that failed (the run goes on; the next run resumes them)."""
    raw_dir = Path(raw_dir)
    base = get_settings().europepmc_preprint_base.rstrip("/")
    session = session or requests.Session()

    listing = session.get(f"{base}/", timeout=_TIMEOUT)
    listing.raise_for_status()
    names = parse_listing(listing.text)
    if not names:
        raise RuntimeError(f"europepmc_preprint: no PPR<a>_PPR<b>.xml.gz files at {base}")

    if dry_run:
        print(f"would fetch {len(names)} bulk files from {base}")
        for n in names:
            print(f"  {n}")
        return {"files": len(names), "fetched": 0, "skipped": 0, "errors": 0}

    raw_dir.mkdir(parents=True, exist_ok=True)
    fetched = skipped = errors = 0
    for name in names:
        dest = raw_dir / name
        url = f"{base}/{name}"
        if not force and dest.is_file() and dest.stat().st_size > 0:
            size = _remote_size(session, url)
            if size is not None and size == dest.stat().st_size:
                skipped += 1
                continue
        # The cap comes first: a file that is not fetched is never touched, also with force.
        if max_files and fetched >= max_files:
            continue
        try:
            _fetch(session, url, dest, fresh=force)
            fetched += 1
        except Exception as exc:  # noqa: BLE001 - one file must not stop the others
            print(f"WARN: {name}: {exc}", file=sys.stderr)
            errors += 1

    print(
        f"europepmc_preprint: files={len(names)} fetched={fetched} "
        f"skipped={skipped} errors={errors}"
    )
    return {"files": len(names), "fetched": fetched, "skipped": skipped, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Europe PMC preprint bulk files download")
    p.add_argument(
        "--raw-dir",
        type=Path,
        default=get_settings().raw_root / "europepmc" / "preprints_bulk",
    )
    p.add_argument("--max-files", type=int, default=0, help="cap on files fetched (0 = no cap)")
    p.add_argument("--force", action="store_true", help="fetch every file again")
    p.add_argument("--dry-run", "--dry_run", dest="dry_run", action="store_true")
    args = p.parse_args(argv)
    res = download_bulk(
        args.raw_dir, max_files=args.max_files, force=args.force, dry_run=args.dry_run
    )
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
