"""Fetch the public reference lists, and EDQM when the owner has set the keys.

LOINC and ISO 3166 are not fetched. The owner places those files.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from urllib.parse import urlsplit

import requests

from episteme.config import get_settings
from episteme.data.refmeta.edqm import (
    PAGE,
    class_codes,
    class_data_uri,
    classes_uri,
    content_rows,
    stapi_key,
    versions_uri,
)

_LOG = logging.getLogger("episteme.refmeta")

ISO639_NOTICE = (
    "Library of Congress ISO 639-2 UTF-8 code list. The Library of Congress "
    "states this file may be loaded into a database. Code elements only. "
    "Project ruling 2026-11-15; not a legal determination."
)

ISO3166_NOTICE = (
    "ISO 3166-1 code elements from the pycountry package (Debian iso-codes data, "
    "LGPL-2.1). ISO states that the code elements may be used, including in "
    "software; the standard text is not included. Project ruling 2026-11-16; "
    "not a legal determination."
)

_ORDER = ("ucum", "iso639", "iso3166", "edqm")


def edqm_notice(retrieved_at: str) -> str:
    """The EDQM Standard Terms conditions of use (web services), as they bind a
    lookup copy: source and permission statement, no independent modification,
    retrieval date and the not-static notice. Text from EDQM, received by the
    owner 2026-11-16."""
    return (
        "Data from the EDQM Standard Terms database (http://standardterms.edqm.eu), "
        "reproduced with the permission of the European Directorate for the Quality "
        "of Medicines & HealthCare, Council of Europe (EDQM).\n"
        f"Retrieved from the Standard Terms database on {retrieved_at}. The EDQM "
        "Standard Terms database is not a static list and content can change over time.\n"
        "The Standard Terms data is not modified independently. No other data is "
        "represented as part of Standard Terms; any data shown alongside it does not "
        "originate from Standard Terms.\n"
        "Access to the Standard Terms APIs is provided free of charge and without guarantee.\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download reference code lists")
    parser.add_argument("--system", action="append", choices=_ORDER)
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--raw-dir", type=Path, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--reason", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.max_files is not None and args.max_files < 1:
        print("refmeta download: --max-files must be at least 1", file=sys.stderr)
        return 2
    raw = args.raw_dir or get_settings().raw_root
    plan = list(args.system or _ORDER)
    if args.max_files is not None:
        plan = plan[: args.max_files]
    explicit = set(args.system or ())
    for system in plan:
        if system == "edqm" and not _edqm_configured():
            if "edqm" in explicit:
                print(
                    "refmeta download: EDQM_STAPI_LOGIN and EDQM_STAPI_SECRET_KEY are unset",
                    file=sys.stderr,
                )
                return 3
            _LOG.info("edqm: keys unset; skipped")
            continue
        _fetch(system, raw, dry_run=args.dry_run, force=args.force)
    return 0


def _edqm_configured() -> bool:
    settings = get_settings()
    return bool(
        settings.edqm_stapi_base and settings.edqm_stapi_login and settings.edqm_stapi_secret_key
    )


def _fetch(system: str, raw: Path, *, dry_run: bool, force: bool) -> None:
    if system == "ucum":
        _fetch_ucum(raw, dry_run=dry_run, force=force)
    elif system == "iso639":
        _fetch_iso639(raw, dry_run=dry_run, force=force)
    elif system == "iso3166":
        _fetch_iso3166(raw, dry_run=dry_run, force=force)
    elif system == "edqm":
        _fetch_edqm(raw, dry_run=dry_run, force=force)
    else:
        raise ValueError(system)


def _fetch_ucum(raw: Path, *, dry_run: bool, force: bool) -> None:
    settings = get_settings()
    if not settings.ucum_essence_url or not settings.ucum_license_url:
        raise SystemExit("UCUM_ESSENCE_URL and UCUM_LICENSE_URL must be set")
    if dry_run:
        _LOG.info("ucum: would fetch the essence file and the licence")
        return
    body = _http_get(settings.ucum_essence_url)
    notice = _http_get(settings.ucum_license_url)
    if not notice.strip():
        raise SystemExit("ucum: the licence file is empty")
    release = hashlib.sha256(body).hexdigest()[:12]
    folder = raw / "refmeta" / "ucum" / release
    if not force and (folder / "ucum-essence.xml").is_file():
        _LOG.info("ucum: %s already stored", release)
        return
    _write(
        folder,
        {
            "ucum-essence.xml": body,
            "NOTICE.txt": notice,
            "PROVENANCE.txt": _provenance("UCUM_ESSENCE_URL", release).encode(),
        },
    )
    _LOG.info("ucum: stored %s", release)


def _fetch_iso639(raw: Path, *, dry_run: bool, force: bool) -> None:
    settings = get_settings()
    if not settings.iso639_2_url:
        raise SystemExit("ISO639_2_URL must be set")
    if dry_run:
        _LOG.info("iso639: would fetch the UTF-8 code list")
        return
    body = _http_get(settings.iso639_2_url)
    release = hashlib.sha256(body).hexdigest()[:12]
    folder = raw / "refmeta" / "iso639" / release
    if not force and (folder / "ISO-639-2_utf-8.txt").is_file():
        _LOG.info("iso639: %s already stored", release)
        return
    _write(
        folder,
        {
            "ISO-639-2_utf-8.txt": body,
            "NOTICE.txt": (ISO639_NOTICE + "\n").encode(),
            "PROVENANCE.txt": _provenance("ISO639_2_URL", release).encode(),
        },
    )
    _LOG.info("iso639: stored %s", release)


def iso3166_csv() -> tuple[str, bytes]:
    """``(pycountry version, codes.csv bytes)``: every current ISO 3166-1 country,
    sorted by alpha-2, in the header the loader expects (``iso3166.HEADER``)."""
    import importlib.metadata

    import pycountry

    from episteme.data.refmeta.iso3166 import HEADER

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(HEADER)
    for c in sorted(pycountry.countries, key=lambda c: c.alpha_2):
        writer.writerow((c.alpha_2, c.alpha_3, c.numeric, c.name))
    return importlib.metadata.version("pycountry"), out.getvalue().encode("utf-8")


def _fetch_iso3166(raw: Path, *, dry_run: bool, force: bool) -> None:
    if dry_run:
        _LOG.info("iso3166: would write the code list of the installed pycountry")
        return
    version, body = iso3166_csv()
    release = f"pycountry-{version}"
    folder = raw / "refmeta" / "iso3166" / release
    if not force and (folder / "codes.csv").is_file():
        _LOG.info("iso3166: %s already stored", release)
        return
    _write(
        folder,
        {
            "codes.csv": body,
            "NOTICE.txt": (ISO3166_NOTICE + "\n").encode(),
            "PROVENANCE.txt": _provenance("pycountry", release).encode(),
        },
    )
    _LOG.info("iso3166: stored %s", release)


def _fetch_edqm(raw: Path, *, dry_run: bool, force: bool) -> None:
    settings = get_settings()
    base = settings.edqm_stapi_base.rstrip("/")
    host = urlsplit(base).netloc
    if dry_run:
        _LOG.info("edqm: would fetch every class from /classes, with full data and tags")
        return
    catalogue = _paged(base, host, classes_uri)
    codes = class_codes(catalogue)
    fetched = {code: _class_document(base, host, code) for code in codes}
    skipped = [code for code, body in fetched.items() if body is None]
    bodies = {code: body for code, body in fetched.items() if body is not None}
    codes = [code for code in codes if code in bodies]
    try:
        versions = _edqm_get(base, versions_uri(), host)
        label = _version_label(versions.decode("utf-8", errors="replace"))
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "error"
        _LOG.info("edqm: versions request failed (%s); release id is a content hash", status)
        versions = b""
        label = None
    digest = hashlib.sha256()
    for code in codes:
        digest.update(code.encode())
        digest.update(bodies[code])
    release = f"{label}-{digest.hexdigest()[:12]}" if label else digest.hexdigest()[:12]
    folder = raw / "refmeta" / "edqm" / release
    if not force and all((folder / f"{code}.json").is_file() for code in codes):
        _LOG.info("edqm: %s already stored", release)
        return
    files = {f"{code}.json": bodies[code] for code in codes}
    files["classes.json"] = json.dumps({"content": catalogue}, ensure_ascii=False).encode()
    if versions:
        files["versions.json"] = versions
    provenance = _provenance("EDQM_STAPI_BASE", release)
    retrieved_at = provenance.split("fetched_at=", 1)[1].strip()
    if skipped:
        provenance += f"skipped_classes={','.join(skipped)} (server error, not retried)\n"
    files["PROVENANCE.txt"] = provenance.encode()
    files["NOTICE.txt"] = edqm_notice(retrieved_at).encode()
    _write(folder, files)
    _LOG.info("edqm: stored %s (%s classes, with the EDQM conditions notice)", release, len(codes))


def _paged(base: str, host: str, uri_for) -> list:
    rows: list = []
    offset = 0
    while True:
        batch = content_rows(_edqm_get(base, uri_for(limit=PAGE, offset=offset), host))
        rows.extend(batch)
        if len(batch) < PAGE:
            return rows
        offset += PAGE
        if offset > 20000:
            raise SystemExit("edqm: a paged list did not end")


def _class_document(base: str, host: str, code: str) -> bytes | None:
    """One class, in one request (``full_data_by_class`` is not paged). EDQM does
    not allow repeated downloads (owner, 2026-11-16): a class that answers a server
    error is skipped, never asked again in the same run; the caller records it.
    (MAP answered 500 with tags=1 on 2026-11-16 and holds no terms.)"""
    try:
        body = _edqm_get(base, class_data_uri(code), host)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status is None or status < 500:
            raise
        _LOG.warning("edqm: class %s answered HTTP %s; skipped (no retry)", code, status)
        return None
    rows = content_rows(body)
    return json.dumps({"content": rows}, ensure_ascii=False).encode()


def _edqm_get(base: str, uri: str, host: str) -> bytes:
    settings = get_settings()
    date = formatdate(usegmt=True)
    # The server signs the path only: a signature over path + query is refused
    # (403); the path-only signature with the query sent is accepted (2026-11-16).
    key = stapi_key(
        settings.edqm_stapi_login or "",
        settings.edqm_stapi_secret_key or "",
        verb="GET",
        uri=urlsplit(uri).path,
        host=host,
        date=date,
    )
    return _http_get(base + uri, {"X-STAPI-KEY": key, "Date": date})


def _version_label(text: str) -> str | None:
    match = re.search(r"<(?:[^>]*:)?version[^>]*>([^<]+)", text, re.IGNORECASE)
    if not match:
        return None
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", match.group(1).strip())
    return label[:40] or None


def _http_get(url: str, headers: dict[str, str] | None = None) -> bytes:
    response = requests.get(url, headers=headers, timeout=120)
    response.raise_for_status()
    return response.content


def _write(folder: Path, files: dict[str, bytes]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        target = folder / name
        tmp = target.with_name(f".{name}.part")
        tmp.write_bytes(data)
        os.replace(tmp, target)


def _provenance(source_key: str, release: str) -> str:
    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"source_key={source_key}\nrelease={release}\nfetched_at={when}\n"


if __name__ == "__main__":
    raise SystemExit(main())
