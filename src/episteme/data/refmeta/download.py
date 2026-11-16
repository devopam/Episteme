"""Fetch the reference lists: UCUM and ISO 639-2 (public files), ISO 3166 (from the
installed pycountry), and EDQM Standard Terms and LOINC when their keys are set.

EDQM and LOINC do not allow (EDQM) or discourage (LOINC) repeated downloads: each
asks for its version first and downloads nothing when that version is stored, and
nothing is retried.
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
import shutil
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

LOINC_NOTICE = (
    "This material contains content from LOINC (http://loinc.org). LOINC is "
    "copyright (c) Regenstrief Institute, Inc. and the Logical Observation "
    "Identifiers Names and Codes (LOINC) Committee and is available at no cost "
    "under the license at http://loinc.org/license. LOINC(R) is a registered "
    "United States trademark of Regenstrief Institute, Inc."
)

_ORDER = ("ucum", "iso639", "iso3166", "edqm", "loinc")
_MD5_CHUNK = 1 << 20
# EDQM classes not fetched through full_data_by_class: MAP is the mapping class
# (served by the /mapping endpoint; it answered 500 with tags=1 and held no terms,
# 2026-11-16). Mappings are deferred (drift log).
EDQM_EXCLUDED_CLASSES = frozenset({"MAP"})


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
        if system in _CREDENTIALS:
            check, keys = _CREDENTIALS[system]
            if not check():
                if system in explicit:
                    print(f"refmeta download: {keys} are unset", file=sys.stderr)
                    return 3
                _LOG.info("%s: keys unset; skipped", system)
                continue
        _fetch(system, raw, dry_run=args.dry_run, force=args.force)
    return 0


def _edqm_configured() -> bool:
    settings = get_settings()
    return bool(
        settings.edqm_stapi_base and settings.edqm_stapi_login and settings.edqm_stapi_secret_key
    )


def _loinc_configured() -> bool:
    settings = get_settings()
    return bool(settings.loinc_api_base and settings.loinc_username and settings.loinc_secret_key)


# Systems that need credentials: (configured?, the env keys named in the message).
_CREDENTIALS = {
    "edqm": (_edqm_configured, "EDQM_STAPI_LOGIN and EDQM_STAPI_SECRET_KEY"),
    "loinc": (_loinc_configured, "LOINC_USERNAME and LOINC_SECRET_KEY"),
}


def _fetch(system: str, raw: Path, *, dry_run: bool, force: bool) -> None:
    if system == "ucum":
        _fetch_ucum(raw, dry_run=dry_run, force=force)
    elif system == "iso639":
        _fetch_iso639(raw, dry_run=dry_run, force=force)
    elif system == "iso3166":
        _fetch_iso3166(raw, dry_run=dry_run, force=force)
    elif system == "edqm":
        _fetch_edqm(raw, dry_run=dry_run, force=force)
    elif system == "loinc":
        _fetch_loinc(raw, dry_run=dry_run, force=force)
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
            "PROVENANCE.txt": _provenance("UCUM_ESSENCE_URL", release)[0].encode(),
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
            "PROVENANCE.txt": _provenance("ISO639_2_URL", release)[0].encode(),
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
            "PROVENANCE.txt": _provenance("pycountry", release)[0].encode(),
        },
    )
    _LOG.info("iso3166: stored %s", release)


def _loinc_auth() -> tuple[str, str]:
    settings = get_settings()
    return settings.loinc_username or "", settings.loinc_secret_key or ""


def loinc_metadata(version: str | None = None) -> dict:
    """``GET /Loinc`` (current release, or ``version``): version, releaseDate,
    numberOfLoincs, downloadUrl, downloadMD5Hash (loinc.org/kb/api/download)."""
    base = get_settings().loinc_api_base.rstrip("/")
    params = {"version": version} if version else None
    response = requests.get(f"{base}/Loinc", params=params, auth=_loinc_auth(), timeout=120)
    response.raise_for_status()
    meta = response.json()
    for key in ("version", "downloadUrl", "downloadMD5Hash"):
        if not meta.get(key):
            raise SystemExit(f"loinc: the metadata has no {key}")
    if not re.fullmatch(r"[0-9]+\.[0-9]+", str(meta["version"])):
        raise SystemExit(f"loinc: unexpected version string {meta['version']!r}")
    # The zip must come from the API host over the same scheme (https): the Basic
    # credentials are sent with it.
    if urlsplit(meta["downloadUrl"])[:2] != urlsplit(base)[:2]:
        raise SystemExit("loinc: downloadUrl is not on the LOINC API host")
    return meta


def _fetch_loinc(raw: Path, *, dry_run: bool, force: bool) -> None:
    """One metadata call; the zip only when that version is not stored yet (LOINC
    asks not to poll more than once a day). The MD5 from the metadata must match;
    a mismatch is refused and never retried in the same run."""
    if dry_run:
        _LOG.info("loinc: would ask /Loinc for the current release and fetch it if new")
        return
    meta = loinc_metadata()
    release = str(meta["version"])
    folder = raw / "refmeta" / "loinc" / release
    zip_name = f"Loinc_{release}.zip"
    if not force and (folder / zip_name).is_file():
        _LOG.info("loinc: %s already stored", release)
        return
    # Build the release in a staging folder beside the system folders (never under
    # refmeta/loinc/, where the loader picks the newest folder), and move it into
    # place only when it is whole: a failed download leaves no release folder.
    staging = raw / "refmeta" / f".loinc-{release}.part"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        digest = hashlib.md5(usedforsecurity=False)
        with requests.get(
            meta["downloadUrl"], auth=_loinc_auth(), stream=True, timeout=600
        ) as resp:
            resp.raise_for_status()
            with (staging / zip_name).open("wb") as fh:
                for chunk in resp.iter_content(_MD5_CHUNK):
                    fh.write(chunk)
                    digest.update(chunk)
        if digest.hexdigest().lower() != str(meta["downloadMD5Hash"]).lower():
            raise SystemExit(f"loinc: MD5 mismatch for {release}; nothing was stored")
        _write(
            staging,
            {
                "metadata.json": json.dumps(meta, indent=2, ensure_ascii=False).encode(),
                "NOTICE.txt": (LOINC_NOTICE + "\n").encode(),
                "PROVENANCE.txt": _provenance("LOINC_API_BASE", release)[0].encode(),
            },
        )
        if folder.exists():  # --force over a stored release
            shutil.rmtree(folder)
        folder.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, folder)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _LOG.info("loinc: stored %s (%s LOINCs)", release, meta.get("numberOfLoincs"))


def _fetch_edqm(raw: Path, *, dry_run: bool, force: bool) -> None:
    settings = get_settings()
    base = settings.edqm_stapi_base.rstrip("/")
    host = urlsplit(base).netloc
    if dry_run:
        _LOG.info("edqm: would ask /versions, then fetch every class only if that is new")
        return
    # Version first (one request): EDQM does not allow repeated downloads, so a
    # stored release with the same /versions answer means nothing is fetched.
    try:
        versions = _edqm_get(base, versions_uri(), host)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "error"
        if not force:
            raise SystemExit(
                f"edqm: /versions failed (HTTP {status}); without a version the download "
                "could repeat a stored release, so nothing was fetched (use --force)"
            ) from exc
        versions = b""
    stored = _edqm_release_with_versions(raw, versions) if versions else None
    if stored is not None and not force:
        _LOG.info("edqm: version unchanged; %s already stored, nothing fetched", stored.name)
        return
    catalogue = _paged(base, host, classes_uri)
    codes = [c for c in class_codes(catalogue) if c not in EDQM_EXCLUDED_CLASSES]
    bodies: dict[str, bytes] = {}
    for code in codes:
        body = _class_document(base, host, code)
        if body is None:
            # A partial release would load as current and silently lose a class.
            raise SystemExit(f"edqm: class {code} failed; no release was written (no retry)")
        bodies[code] = body
    digest = hashlib.sha256()
    for code in codes:
        digest.update(code.encode())
        digest.update(bodies[code])
    release = digest.hexdigest()[:12]
    folder = raw / "refmeta" / "edqm" / release
    files = {f"{code}.json": bodies[code] for code in codes}
    files["classes.json"] = json.dumps({"content": catalogue}, ensure_ascii=False).encode()
    if versions:
        files["versions.json"] = versions
    provenance, retrieved_at = _provenance("EDQM_STAPI_BASE", release)
    files["PROVENANCE.txt"] = provenance.encode()
    files["NOTICE.txt"] = edqm_notice(retrieved_at).encode()
    _write(folder, files)
    _LOG.info("edqm: stored %s (%s classes, with the EDQM conditions notice)", release, len(codes))


def _edqm_release_with_versions(raw: Path, versions: bytes) -> Path | None:
    """The stored EDQM release whose ``versions.json`` equals ``versions``, if any."""
    root = raw / "refmeta" / "edqm"
    if not root.is_dir():
        return None
    for folder in sorted(root.iterdir()):
        stored = folder / "versions.json"
        if folder.is_dir() and stored.is_file() and stored.read_bytes() == versions:
            return folder
    return None


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


def _provenance(source_key: str, release: str) -> tuple[str, str]:
    """``(PROVENANCE.txt text, fetched_at)``."""
    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"source_key={source_key}\nrelease={release}\nfetched_at={when}\n", when


if __name__ == "__main__":
    raise SystemExit(main())
