"""Paced, resumable download of official reference documents.

Three sources, one downloader:

* ``ich``: ICH guideline files. The ICH site is a single-page app; its node API lists each
  guideline with ``code``, ``status``, ``details.stepDate`` and ``fileGroups[].files[]``. Links
  that sit inside the page text (the E2B(R3) package page) are read too.
* ``meddra_ptc``: the MedDRA Points to Consider documents, from the guidance API.
* ``ema_guidance``: EMA documents, from a fixed list of paths (``ema_documents.txt``). EMA
  answers a client that is not a browser with HTTP 429 at low rates. The pause between two
  requests is long (``ema_doc_min_interval_s``), ``Retry-After`` is honoured, and the stage
  stops with exit code 3 so that a later run resumes. This module never uses a browser to get
  round the limit and never requests ``/search/`` (robots.txt, CAPTCHA).

A file is written to ``<raw_dir>/<source>/<rel_path>`` through a ``.part`` file. Every stored file
has one line in ``<raw_dir>/<source>/_manifest.jsonl`` (source, url, rel_path, sha256, bytes,
fetched_at, version, step_date, licence, input_key). A file whose url is in the manifest, with
the local file intact, is skipped without a request. ``refresh=True`` asks upstream again: an
unchanged file is skipped, a changed file (same url, new sha256) is kept as a new file with a
``.<yyyymmdd>`` suffix and the old file stays. A ``.zip`` is unpacked to a sibling directory
named after the zip stem; a member that leaves that directory (zip-slip) is refused.

``fetch`` returns 0 when done, 3 when it stops for a rate limit, a human-verification page or the
time budget (run again to resume), and 1 when it finished but some files failed.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import stat
import sys
import time
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import requests

from episteme.config import get_settings, source_url
from episteme.data.checkpoint_markers import input_key

_LOG = logging.getLogger(__name__)

SOURCES = ("ich", "meddra_ptc", "ema_guidance")
LICENCE_ICH = (
    "ICH public licence: use, reproduce, adapt with ICH credit; mark every change; "
    "no implied ICH endorsement; third-party content excluded"
)
LICENCE_EMA = "EMA legal notice: reuse allowed with EMA credited; third-party content excluded"
MANIFEST_NAME = "_manifest.jsonl"
EMA_DOCUMENT_ROOT = "/en/documents/"
EMA_PATHS_FILE = Path(__file__).with_name("ema_documents.txt")

# File types a guideline page may list that are documents; video and audio are left out.
_DOCUMENT_SUFFIXES = {
    ".pdf",
    ".zip",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".xml",
    ".csv",
    ".txt",
    ".xsd",
    ".json",
}
_CHUNK = 1 << 20
_CONNECT_TIMEOUT_S = 30
_READ_TIMEOUT_S = 120
_SLEEP_SLICE_S = 1.0
MAX_UNPACKED_BYTES = 2 * 1024**3
DEFAULT_MAX_WAIT_S = 900.0
_FALLBACK_THROTTLE_WAIT_S = 30.0


class ZipSlipError(ValueError):
    """A zip member would be written outside the unpack directory (or is a link, or too large)."""


@dataclass(frozen=True)
class DocRef:
    """One document to fetch."""

    source: str
    url: str
    rel_path: str
    title: str
    version: str | None
    step_date: str | None
    licence: str


# ----------------------------------------------------------------------------- planning


def _safe_name(raw: str) -> str:
    """A file name from the last part of a URL path: unquoted, with unsafe characters replaced."""
    name = unquote(PurePosixPath(urlsplit(raw).path).name)
    name = re.sub(r"[^\w.()\-]+", "_", name).strip("._")
    return name or "document"


def _safe_dir(raw: str, limit: int = 40) -> str:
    return re.sub(r"[^\w.\-]+", "_", raw).strip("._-")[:limit].rstrip("_-") or "misc"


def _is_document(url: str, mimetype: str = "") -> bool:
    if mimetype.startswith(("video/", "audio/")):
        return False
    return PurePosixPath(urlsplit(url).path).suffix.lower() in _DOCUMENT_SUFFIXES


def _get_json(session: Any, url: str, headers: dict[str, str]) -> Any:
    resp = session.get(url, headers=headers, timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S))
    try:
        if resp.status_code != 200:
            raise RuntimeError(f"{url}: HTTP {resp.status_code}")
        return resp.json()
    finally:
        close = getattr(resp, "close", None)
        if close is not None:
            close()


def _headers() -> dict[str, str]:
    return {"User-Agent": get_settings().reference_docs_user_agent}


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _url_allowed(url: str, hosts: set[str]) -> bool:
    """True when the url is http(s), on one of ``hosts``, and not under ``/search``."""
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and _host(url) in hosts and "/search" not in parts.path


def _distinct(rel: str, url: str, used: dict[str, str]) -> str:
    """``rel`` itself, or, when another url already holds it, ``rel`` with a short hash of the
    url before the extension. ``used`` maps the lower-case path to the url."""
    if used.get(rel.lower(), url) != url:
        p = PurePosixPath(rel)
        tag = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
        rel = str(p.with_name(f"{p.stem}__{tag}{p.suffix}"))
    used[rel.lower()] = url
    return rel


_ANCHOR = re.compile(r'<a\s[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)


def plan_ich(
    session: Any,
    pages: Iterable[str],
    *,
    api_base: str | None = None,
    files_base: str | None = None,
    pause_s: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[DocRef]:
    """Read the ICH node API for each page and list the document files.

    A guideline entry (``code`` with ``fileGroups``) gives one ref per file, stored under a
    directory named after its code. A link inside a ``text`` field gives one ref under a
    directory named after the page. A url that appears twice is listed once. Only the hosts
    of ``ICH_API_BASE`` and ``ICH_FILES_BASE`` are kept; a link to another host is logged and
    dropped, and so is any path under ``/search``. Two urls with one file name in one folder
    get distinct names.
    """
    api = (api_base or source_url("ICH_API_BASE")).rstrip("/")
    files = files_base or source_url("ICH_FILES_BASE")
    hosts = {_host(api), _host(files)}
    pause = get_settings().reference_docs_api_pause_s if pause_s is None else pause_s
    refs: list[DocRef] = []
    seen: set[str] = set()
    used: dict[str, str] = {}

    def add(uri: str, directory: str, title: str, version: str | None, step: str | None) -> None:
        url = urljoin(files.rstrip("/") + "/", uri)
        if url in seen:
            return
        seen.add(url)
        if not _url_allowed(url, hosts):
            _LOG.warning("ICH plan: dropped %s (host or path not allowed)", url)
            return
        refs.append(
            DocRef(
                source="ich",
                url=url,
                rel_path=_distinct(f"{directory}/{_safe_name(url)}", url, used),
                title=title,
                version=version,
                step_date=step,
                licence=LICENCE_ICH,
            )
        )

    def walk(node: Any, page_dir: str) -> None:
        if isinstance(node, dict):
            if "fileGroups" in node and "code" in node:
                code = _safe_dir(str(node["code"]), 60)
                details = node.get("details") or {}
                step = details.get("stepDate") if isinstance(details, dict) else None
                status = node.get("status") or None
                for group in node.get("fileGroups") or []:
                    for f in group.get("files") or []:
                        uri = f.get("uri")
                        if uri and _is_document(uri, str(f.get("mimetype") or "")):
                            add(uri, code, f.get("title") or _safe_name(uri), status, step or None)
                return
            for key, value in node.items():
                if key == "text" and isinstance(value, str):
                    for href, inner in _ANCHOR.findall(value):
                        href = html.unescape(href)
                        if _is_document(href):
                            add(href, page_dir, _plain(inner) or _safe_name(href), None, None)
                else:
                    walk(value, page_dir)
        elif isinstance(node, list):
            for item in node:
                walk(item, page_dir)

    for number, page in enumerate(pages):
        if number:
            sleep(pause)
        url = f"{api}/nodes?alias=/page/{page}&loadEntities[]=paragraph"
        walk(_get_json(session, url, _headers()), f"page_{_safe_dir(page)}")
    return refs


def _release(text: str | None) -> str | None:
    m = re.search(r"Release\s+(\d+(?:\.\d+)*)", text or "")
    return m.group(1) if m else None


def plan_meddra(
    session: Any, *, api_base: str | None = None, taxonomy: str | None = None
) -> list[DocRef]:
    """List the English Points to Consider documents and the best-practices document.

    Only the item that holds the Points to Consider is read. The per-version items (release
    notes, introductory guides) are left out: their licence is not checked. The HTML renderings
    sit on a third-party host and are left out too; the Word, PDF and spreadsheet files stay.
    Only the host of ``MEDDRA_API_BASE`` is kept; any path under ``/search`` is dropped.
    """
    api = (api_base or source_url("MEDDRA_API_BASE")).rstrip("/")
    tax = taxonomy or source_url("MEDDRA_PTC_TAXONOMY")
    payload = _get_json(session, f"{api}/guidances-page?taxonomy={tax}&_format=json", _headers())
    languages = payload if isinstance(payload, list) else [payload]
    hosts = {_host(api)}
    refs: list[DocRef] = []
    seen: set[str] = set()
    used: dict[str, str] = {}

    def add(url: str, title: str, version: str | None, date: str | None) -> None:
        if url in seen:
            return
        seen.add(url)
        if not _url_allowed(url, hosts):
            _LOG.warning("MedDRA plan: dropped %s (host or path not allowed)", url)
            return
        rel = _distinct(_safe_name(url), url, used)
        refs.append(DocRef("meddra_ptc", url, rel, title, version, date, LICENCE_ICH))

    for block in languages:
        if not isinstance(block, dict):
            continue
        if str(block.get("language", "English")).strip().lower() != "english":
            continue
        for item in block.get("items") or []:
            if "points to consider" not in str(item.get("title") or "").lower():
                continue
            date = item.get("date") or None
            for doc in item.get("documentCollection") or []:
                title = doc.get("documentTitle") or str(item.get("title"))
                for key in ("guidanceFilePdf", "guidanceFileDoc", "guidanceFileSpreadsheet"):
                    f = doc.get(key)
                    if isinstance(f, dict) and f.get("url"):
                        add(f["url"], str(title).strip(), _release(title), date)
            for f in item.get("otherDocuments") or []:
                if isinstance(f, dict) and f.get("url"):
                    title = f.get("description") or _safe_name(f["url"])
                    add(f["url"], str(title).strip(), _release(title), date)
    return refs


def plan_ema(paths_file: Path | None = None, *, base: str | None = None) -> list[DocRef]:
    """One ref per path of the list file (paths only, no host). A path outside
    ``/en/documents/`` or under ``/search`` is refused."""
    root = (base or source_url("EMA_BASE")).rstrip("/")
    path = Path(paths_file) if paths_file else EMA_PATHS_FILE
    refs: list[DocRef] = []
    seen: set[str] = set()
    used: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(EMA_DOCUMENT_ROOT) or "/search" in line or "://" in line:
            raise ValueError(f"EMA path outside {EMA_DOCUMENT_ROOT}: {line!r}")
        if line in seen:
            continue
        seen.add(line)
        name = _safe_name(line)
        title = re.sub(r"_en$", "", PurePosixPath(name).stem).replace("-", " ")
        url = root + line
        name = _distinct(name, url, used)
        refs.append(DocRef("ema_guidance", url, name, title, None, None, LICENCE_EMA))
    return refs


# ----------------------------------------------------------------------------- zip


def extract_zip(zip_path: Path, dest_dir: Path) -> int:
    """Unpack ``zip_path`` into ``dest_dir`` and return the member count.

    Every member name is checked first; a name that is absolute, has a drive, climbs out with
    ``..`` or resolves outside ``dest_dir``, a link member, or an archive that unpacks to more
    than ``MAX_UNPACKED_BYTES`` raises ``ZipSlipError`` before any file is written."""
    dest_root = Path(dest_dir).resolve()
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        total = 0
        plan: list[tuple[zipfile.ZipInfo, Path]] = []
        for info in infos:
            name = info.filename.replace("\\", "/")
            parts = PurePosixPath(name).parts
            if (
                name.startswith("/")
                or ".." in parts
                or (parts and re.match(r"^[A-Za-z]:", parts[0]))
                or "\x00" in name
            ):
                raise ZipSlipError(f"unsafe member path {info.filename!r} in {zip_path.name}")
            target = (dest_root / Path(*parts)).resolve() if parts else dest_root
            try:
                target.relative_to(dest_root)
            except ValueError as exc:
                raise ZipSlipError(
                    f"member {info.filename!r} of {zip_path.name} leaves the unpack directory"
                ) from exc
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ZipSlipError(f"link member {info.filename!r} in {zip_path.name}")
            total += info.file_size
            if total > MAX_UNPACKED_BYTES:
                raise ZipSlipError(
                    f"{zip_path.name} unpacks to more than {MAX_UNPACKED_BYTES} bytes"
                )
            plan.append((info, target))
        dest_root.mkdir(parents=True, exist_ok=True)
        for info, target in plan:
            if info.is_dir() or info.filename.endswith(("/", "\\")):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                while chunk := src.read(_CHUNK):
                    out.write(chunk)
    return len(infos)


# ----------------------------------------------------------------------------- fetch


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sliced_sleep(seconds: float) -> None:
    """Sleep in slices, so a pending stop (an interrupt cannot wake one long sleep) is seen."""
    left = seconds
    while left > 0:
        piece = min(_SLEEP_SLICE_S, left)
        time.sleep(piece)
        left -= piece


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _parse_retry_after(value: Any) -> float | None:
    from episteme.data.regulatory.download_ema import parse_retry_after

    return parse_retry_after(value)


def _read_manifest(path: Path) -> dict[str, dict[str, Any]]:
    """The latest record per url."""
    latest: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return latest
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            _LOG.warning("%s line %d is not JSON; ignored", path.name, number)
            continue
        if isinstance(rec, dict) and rec.get("url"):
            latest[rec["url"]] = rec
    return latest


def _append_manifest(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _destination(raw_dir: Path, ref: DocRef) -> Path:
    base = (Path(raw_dir) / ref.source).resolve()
    dest = (base / ref.rel_path).resolve()
    try:
        dest.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"rel_path leaves the source directory: {ref.rel_path!r}") from exc
    if dest.name == MANIFEST_NAME or dest.name.endswith(".part"):
        raise ValueError(f"rel_path uses a reserved name: {ref.rel_path!r}")
    return dest


def _versioned(dest: Path, stamp: str) -> Path:
    cand = dest.with_name(f"{dest.stem}.{stamp}{dest.suffix}")
    n = 2
    while cand.exists():
        cand = dest.with_name(f"{dest.stem}.{stamp}-{n}{dest.suffix}")
        n += 1
    return cand


def _rel(dest: Path, source_dir: Path) -> str:
    return dest.relative_to(source_dir).as_posix()


class _Stop(Exception):
    """Rate limit, block, verification page, EMA connection error or budget: fetch returns 3."""


class _NetError(Exception):
    """A connection error or timeout (a ``requests.RequestException`` of the session)."""


def fetch(
    refs: list[DocRef],
    raw_dir: Path,
    *,
    session: Any,
    min_interval_s: float,
    budget_s: float | None,
    refresh: bool = False,
    max_wait_s: float = DEFAULT_MAX_WAIT_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = _sliced_sleep,
) -> int:
    """Fetch ``refs`` one at a time. Returns 0 (done), 3 (stopped; run again to resume) or 1
    (done, but some files failed). See the module text."""
    raw_dir = Path(raw_dir)
    keys: dict[str, str] = {}
    for ref in refs:
        if "/search" in urlsplit(ref.url).path:
            raise ValueError(f"a /search url is never fetched: {ref.url}")
        dest = _destination(raw_dir, ref)
        key = input_key(dest, raw_dir)
        if key in keys and keys[key] != ref.url:
            raise ValueError(f"input_key collision {key!r}: {keys[key]} and {ref.url}")
        keys[key] = ref.url
    headers = _headers()
    start = clock()
    state: dict[str, Any] = {"last": None}
    manifests: dict[str, dict[str, dict[str, Any]]] = {}
    failed: list[str] = []

    def pace() -> None:
        wait = 0.0
        if state["last"] is not None:
            wait = max(0.0, min_interval_s - (clock() - state["last"]))
        if budget_s is not None and (clock() - start) + wait >= budget_s:
            raise _Stop("time budget used")
        if wait > 0:
            sleep(wait)

    def request(ref: DocRef) -> Any:
        """The response of a 200, after pacing and one wait on a throttle."""
        throttles = 0
        while True:
            pace()
            try:
                resp = session.get(
                    ref.url,
                    headers=headers,
                    stream=True,
                    timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
                )
            except requests.RequestException as exc:
                state["last"] = clock()
                raise _NetError(str(exc)) from exc
            state["last"] = clock()
            if resp.status_code not in (429, 503):
                return resp
            resp.close()
            throttles += 1
            wait = _parse_retry_after(resp.headers.get("Retry-After"))
            if wait is None:
                wait = max(min_interval_s, _FALLBACK_THROTTLE_WAIT_S)
            wait = max(wait, min_interval_s)
            if throttles >= 2:
                raise _Stop(f"HTTP {resp.status_code} twice for {ref.url}")
            if wait > max_wait_s:
                raise _Stop(f"HTTP {resp.status_code}; Retry-After {wait:.0f} s is over the cap")
            if budget_s is not None and (clock() - start) + wait >= budget_s:
                raise _Stop(f"HTTP {resp.status_code}; the wait does not fit the budget")
            _LOG.warning("HTTP %d for %s; waiting %.0f s", resp.status_code, ref.url, wait)
            sleep(wait)
            state["last"] = None  # the wait was the pause

    def download(ref: DocRef, part: Path) -> tuple[str, int] | None:
        """Write the body to ``part``; return (sha256, bytes), or None when the file failed."""
        ema = ref.source == "ema_guidance"
        resp = None
        try:
            try:
                resp = request(ref)
            except _NetError as exc:
                if ema:
                    raise _Stop(f"connection error for {ref.url}: {exc}") from exc
                _LOG.error("request failed for %s: %s", ref.url, exc)
                return None
            if resp.status_code == 403 and ema:
                raise _Stop(f"HTTP 403 for {ref.url}: EMA blocks this client")
            if resp.status_code != 200:
                _LOG.error("HTTP %d for %s", resp.status_code, ref.url)
                return None
            ctype = str(resp.headers.get("Content-Type", "")).lower()
            if ctype.startswith("text/html") and not ref.rel_path.lower().endswith(
                (".htm", ".html")
            ):
                if ema:
                    raise _Stop(
                        f"an HTML page instead of a document for {ref.url} (verification page?)"
                    )
                _LOG.error("HTML page instead of a document for %s", ref.url)
                return None
            h = hashlib.sha256()
            size = 0
            part.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(part, "wb") as out:
                    for chunk in resp.iter_content(chunk_size=_CHUNK):
                        if chunk:
                            out.write(chunk)
                            h.update(chunk)
                            size += len(chunk)
            except requests.RequestException as exc:
                if ema:
                    raise _Stop(f"transfer failed for {ref.url}: {exc}") from exc
                _LOG.error("transfer failed for %s: %s", ref.url, exc)
                return None
            declared = resp.headers.get("Content-Length")
            # With Content-Encoding set, requests writes the decoded bytes and Content-Length
            # counts the encoded ones: the two cannot be compared (as in download_ema.py).
            encoded = bool(str(resp.headers.get("Content-Encoding", "")).strip())
            if not encoded and declared and str(declared).isdigit() and int(declared) != size:
                _LOG.error("short read for %s: %d of %s bytes", ref.url, size, declared)
                return None
            return h.hexdigest(), size
        finally:
            close = getattr(resp, "close", None)
            if close is not None:
                close()

    def unpack(dest: Path) -> int | None:
        if dest.suffix.lower() != ".zip":
            return None
        return extract_zip(dest, dest.with_name(dest.stem))

    def intact(source_dir: Path, rec: dict[str, Any]) -> bool:
        path = source_dir / str(rec.get("rel_path", ""))
        try:
            return (
                path.is_file()
                and path.stat().st_size == rec.get("bytes")
                and _sha256_file(path) == rec.get("sha256")
            )
        except OSError:
            return False

    def record(
        ref: DocRef, target: Path, sha: str, size: int, supersedes: str | None, *, adopted: bool
    ) -> bool:
        """Unpack a zip, then write the manifest line. False when the zip is refused."""
        try:
            members = unpack(target)
        except (ZipSlipError, zipfile.BadZipFile) as exc:
            _LOG.error("refused %s: %s", ref.url, exc)
            if not adopted:  # a file put there by hand is never deleted
                target.unlink(missing_ok=True)
            return False
        source_dir = (raw_dir / ref.source).resolve()
        rec = {
            "source": ref.source,
            "url": ref.url,
            "rel_path": _rel(target, source_dir),
            "title": ref.title,
            "sha256": sha,
            "bytes": size,
            "fetched_at": _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "version": ref.version,
            "step_date": ref.step_date,
            "licence": ref.licence,
            "input_key": input_key(target, raw_dir),
            "members": members,
            "supersedes": supersedes,
            "adopted": adopted,
        }
        _append_manifest(source_dir / MANIFEST_NAME, rec)
        manifests[ref.source][ref.url] = rec
        return True

    try:
        for ref in refs:
            dest = _destination(raw_dir, ref)
            source_dir = (raw_dir / ref.source).resolve()
            manifest_path = source_dir / MANIFEST_NAME
            latest = manifests.setdefault(ref.source, _read_manifest(manifest_path))
            known = latest.get(ref.url)
            if known and not refresh and intact(source_dir, known):
                _LOG.info("skip (in manifest): %s", ref.rel_path)
                kept = source_dir / str(known["rel_path"])
                if kept.suffix.lower() == ".zip" and not kept.with_name(kept.stem).is_dir():
                    try:
                        unpack(kept)  # the unpacked folder was deleted: make it again
                    except (ZipSlipError, zipfile.BadZipFile) as exc:
                        _LOG.error("refused %s: %s", ref.url, exc)
                        failed.append(ref.url)
                continue
            if not known and not refresh and dest.is_file():
                # A file put there by hand (or by an earlier layout): adopt it by sha256,
                # with no request.
                _LOG.info("adopt (on disk, not in manifest): %s", ref.rel_path)
                if not record(
                    ref, dest, _sha256_file(dest), dest.stat().st_size, None, adopted=True
                ):
                    failed.append(ref.url)
                continue
            part = dest.with_name(dest.name + ".part")
            got = download(ref, part)
            if got is None:
                part.unlink(missing_ok=True)
                failed.append(ref.url)
                continue
            sha, size = got
            supersedes = None
            if known:
                prior = source_dir / str(known["rel_path"])
                if known.get("sha256") == sha:
                    if not intact(source_dir, known):  # the stored file is gone or damaged
                        os.replace(part, prior)
                        try:
                            unpack(prior)
                        except (ZipSlipError, zipfile.BadZipFile) as exc:
                            _LOG.error("refused %s: %s", ref.url, exc)
                            failed.append(ref.url)
                    else:
                        part.unlink(missing_ok=True)
                    continue
                target = _versioned(dest, _utcnow().strftime("%Y%m%d"))
                supersedes = known["rel_path"]
                _LOG.warning(
                    "version change: %s now has sha256 %s (was %s); kept as %s",
                    ref.url,
                    sha,
                    known.get("sha256"),
                    target.name,
                )
            elif dest.exists():
                if _sha256_file(dest) == sha:  # a file put there by hand: adopt it
                    part.unlink(missing_ok=True)
                    target = dest
                else:
                    target = _versioned(dest, _utcnow().strftime("%Y%m%d"))
                    supersedes = _rel(dest, source_dir)
                    _LOG.warning(
                        "%s differs from the file on disk; kept as %s", ref.url, target.name
                    )
            else:
                target = dest
            if part.exists():
                os.replace(part, target)
            if not record(ref, target, sha, size, supersedes, adopted=False):
                failed.append(ref.url)
    except _Stop as stop:
        _LOG.warning("stopping: %s; fetched files are kept; run again to resume", stop)
        return 3
    return 1 if failed else 0


# ----------------------------------------------------------------------------- command line


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    p.add_argument("source", choices=SOURCES)
    p.add_argument("--dry-run", action="store_true", help="plan and print; fetch nothing")
    p.add_argument("--max-files", type=int, default=None, help="plan at most N files")
    p.add_argument("--budget-s", type=float, default=None, help="stop (exit 3) after N seconds")
    p.add_argument("--refresh", action="store_true", help="ask upstream again for known files")
    p.add_argument("--output-dir", type=Path, default=None, help="raw root (default: settings)")
    return p


def plan(source: str, session: Any) -> list[DocRef]:
    settings = get_settings()
    if source == "ich":
        return plan_ich(session, settings.reference_docs_ich_pages)
    if source == "meddra_ptc":
        return plan_meddra(session)
    return plan_ema()


def main(argv: list[str] | None = None) -> int:
    from episteme.ops.stop import stage_guard

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_parser().parse_args(argv)
    settings = get_settings()
    raw_dir = args.output_dir or settings.raw_root
    interval = (
        float(settings.ema_doc_min_interval_s)
        if args.source == "ema_guidance"
        else settings.reference_docs_min_interval_s
    )
    session = requests.Session()
    refs = plan(args.source, session)
    if args.max_files is not None:
        refs = refs[: args.max_files]
    if args.dry_run:
        for ref in refs:
            print(f"dry-run: would fetch {ref.url} -> {raw_dir}/{ref.source}/{ref.rel_path}")
        return 0
    with stage_guard(lambda: None, argv=argv) as g:
        rc = fetch(
            refs,
            raw_dir,
            session=session,
            min_interval_s=interval,
            budget_s=args.budget_s,
            refresh=args.refresh,
        )
        print(f"{args.source}: planned={len(refs)} exit={rc}")
        return g.exit_code(rc)


if __name__ == "__main__":
    sys.exit(main())
