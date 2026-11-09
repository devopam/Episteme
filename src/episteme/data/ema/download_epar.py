"""Download the English EPAR PDFs that the ``ema_epar_documents`` feed lists.

The feed (fetched by ``episteme.data.regulatory.download_ema``) is data, so nothing in it
is trusted: a row is used only when its ``type`` is in ``feeds.toml`` (``document_types``),
its ``id`` is a plain number (it names the stored file) and its ``document_url`` starts
with the prefix in ``sources.env`` (``EMA_EPAR_DOCUMENT_PREFIX``) and ends ``_en.pdf``.
Any other row is refused and counted; no request is made for it.

One file at a time, ``EPISTEME_EMA_DELAY_SECONDS`` between requests. The stored name is
``<raw dir>/epar/<id>.pdf`` (never taken from the URL), written to a temporary name and
renamed when the bytes are a whole PDF. Each document has a success marker keyed on
``input_key`` of the stored file, with the feed's ``last_updated_date`` and the sha256 of
the bytes; a re-run skips a document whose file and marker are there and whose
``last_updated_date`` is unchanged, and downloads again (replacing the file) when it
changed.

A document over ``EPISTEME_EPAR_MAX_BYTES`` (the cap the extract reads to) is stopped as
soon as the declared length or the stream passes the cap: the partial file is deleted, the
document gets a failed marker (``over_limit``), it is not retried, and the others go on.

EMA answers automated requests with a human-verification page under some conditions. Only
status 200 with HTML (or any 200 body that is not a PDF) is that page: it is never retried.
The stage writes a failed marker and stops with ``FeedVerificationError``, keeps every file
already fetched, and a re-run resumes. A download cut short is tried once more and then
recorded as failed; the other documents still run.

A 429 or 503 is a throttle, not a failure of the document: the whole run pauses
(``Retry-After`` when EMA sends it, else a backoff that doubles per throttle in a row), and
the same document is asked for again without using one of its attempts. After
``EPISTEME_EMA_MAX_THROTTLES`` throttles in a row the stage writes a failed marker, keeps
every file already fetched, and stops with ``RateLimited`` (exit 3); a re-run resumes. A
stop request or a dead parent ends the stage cleanly (``stage_guard``, no lock).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import requests

from episteme.config import get_settings, source_url
from episteme.data.checkpoint_markers import (
    failed_marker_path,
    input_key,
    mark_failed,
    mark_success,
    success_marker_path,
)
from episteme.data.regulatory.download_ema import (
    ATTEMPTS,
    FeedVerificationError,
    RateLimited,
    Retryable,
    Throttled,
    ThrottleGate,
    TooLarge,
    download_to,
    drop_success_marker,
    remove_stale_temps,
)
from episteme.data.regulatory.feeds import load_feeds
from episteme.data.regulatory.parsers import ema_total, parse
from episteme.ops.stop import stage_guard

_LOG = logging.getLogger(__name__)

__all__ = [
    "ATTEMPTS",
    "FeedVerificationError",
    "RateLimited",
    "fetch_documents",
    "select_documents",
]

MARKER_SOURCE = "ema_epar_download"
FEED_NAME = "ema_epar_documents"
FEED_FILE = f"{FEED_NAME}.json"
SUBDIR = "epar"
PREFIX_KEY = "EMA_EPAR_DOCUMENT_PREFIX"
# A document id names the stored file: digits only, no leading zero, so one document has
# one name and no feed value can reach outside the folder.
_ID_RE = re.compile(r"^(0|[1-9][0-9]{0,11})$")
_PDF_HEAD = 1024
_PDF_TAIL = 4096


def document_path(raw_dir: Path, doc_id: str) -> Path:
    return Path(raw_dir) / SUBDIR / f"{doc_id}.pdf"


def _id_ok(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def _url_ok(value: Any, prefix: str) -> bool:
    return isinstance(value, str) and value.startswith(prefix) and value.endswith("_en.pdf")


def select_documents(
    rows: Iterable[dict],
    types: tuple[str, ...],
    *,
    prefix: str | None = None,
    stats: dict | None = None,
) -> Iterator[dict]:
    """The rows whose ``type`` is in ``types``, with a plain-number ``id`` and an English
    PDF ``document_url`` under ``prefix`` (default: ``sources.env`` key
    ``EMA_EPAR_DOCUMENT_PREFIX``). ``stats`` (a dict) gets the counts ``rows``,
    ``other_type``, ``refused_id`` and ``refused_url`` when the iteration ends. Streaming:
    no row is held."""
    prefix = source_url(PREFIX_KEY) if prefix is None else prefix
    counts: Counter = Counter()
    try:
        for row in rows:
            counts["rows"] += 1
            if row.get("type") not in types:
                counts["other_type"] += 1
                continue
            if not _id_ok(row.get("id")):
                counts["refused_id"] += 1
                continue
            if not _url_ok(row.get("document_url"), prefix):
                counts["refused_url"] += 1
                continue
            yield row
    finally:
        if stats is not None:
            stats.update(counts)


def latest_documents(docs: Iterable[dict], *, stats: dict | None = None) -> list[dict]:
    """One document per ``id`` (the one with the latest ``last_updated_date``; a tie keeps
    the first), in number order. ``stats["duplicate_ids"]`` counts the rows dropped."""
    best: dict[str, dict] = {}
    dropped = 0
    for d in docs:
        cur = best.get(d["id"])
        if cur is None:
            best[d["id"]] = d
            continue
        dropped += 1
        if str(d.get("last_updated_date") or "") > str(cur.get("last_updated_date") or ""):
            best[d["id"]] = d
    if stats is not None:
        stats["duplicate_ids"] = dropped
    return [best[i] for i in sorted(best, key=int)]


def load_documents(
    feed_path: Path, types: tuple[str, ...] | None = None, *, stats: dict | None = None
) -> list[dict]:
    """The documents to fetch or read: the feed file, selected and deduplicated.
    ``stats`` gets the counts of ``select_documents``, ``duplicate_ids``, ``feed_rows`` and
    ``total_records`` (``meta.total_records``, or ``None``)."""
    feed = load_feeds()[FEED_NAME]
    types = feed.document_types if types is None else types
    st: dict = {}
    selected = list(select_documents(parse(feed, Path(feed_path)), types, stats=st))
    docs = latest_documents(selected, stats=st)
    st["feed_rows"] = st.get("rows", 0)
    st["total_records"] = ema_total(Path(feed_path))
    if stats is not None:
        stats.update(st)
    return docs


def _check_pdf(tmp: Path) -> dict[str, Any]:
    """Prove the bytes are a whole PDF. A 200 body that does not start like a PDF is the
    verification page (or something as unwanted): ``FeedVerificationError``. A PDF without
    its end marker is cut short: ``Retryable``."""
    with open(tmp, "rb") as fh:
        head = fh.read(_PDF_HEAD)
        if b"%PDF-" not in head:
            raise FeedVerificationError(
                "EMA answered status 200 with a body that is not a PDF: "
                "its human-verification page, or a changed site; the stage stops"
            )
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - _PDF_TAIL))
        if b"%%EOF" not in fh.read():
            raise Retryable(f"truncated PDF: no end marker in {size} bytes")
        fh.seek(0)
        sha = hashlib.sha256()
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)
    return {"sha256": sha.hexdigest()}


def _marker_updated(processed_root: Path, key: str) -> str | None:
    try:
        payload = json.loads(
            success_marker_path(processed_root, MARKER_SOURCE, key).read_text(encoding="utf-8")
        )
        return str(payload["stats"]["last_updated"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def fetch_documents(
    raw_dir: Path,
    docs: Iterable[dict],
    *,
    delay_s: float | None = None,
    session: Any = None,
    processed_root: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
    force: bool = False,
    max_files: int | None = None,
    backoff_s: float | None = None,
    max_backoff_s: float | None = None,
    max_throttles: int | None = None,
) -> dict:
    """Download the English PDF of each document into ``raw_dir/epar/<id>.pdf``.

    Returns ``{"fetched": [ids], "skipped": [ids], "failed": {id: reason}, "refused": n}``.
    Raises ``FeedVerificationError`` (after its failed marker) when EMA answers with its
    verification page, and ``RateLimited`` (after its failed marker) after too many throttles
    in a row; what was fetched before stays. ``session`` and ``sleep`` are seams
    for tests. A document that ``select_documents`` would refuse (id, URL prefix) is
    refused here too: the caller need not have filtered."""
    raw_dir = Path(raw_dir)
    settings = get_settings()
    processed_root = Path(processed_root) if processed_root else settings.processed_root
    delay = settings.ema_delay_seconds if delay_s is None else delay_s
    max_bytes = settings.epar_max_bytes
    prefix = source_url(PREFIX_KEY)
    session = session or requests.Session()
    gate = ThrottleGate(
        sleep=sleep, backoff_s=backoff_s, max_backoff_s=max_backoff_s, max_throttles=max_throttles
    )

    result: dict = {"fetched": [], "skipped": [], "failed": {}, "refused": 0}
    selected: list[dict] = []
    for d in docs:
        if not (_id_ok(d.get("id")) and _url_ok(d.get("document_url"), prefix)):
            result["refused"] += 1
            continue
        selected.append(d)
    if max_files is not None:
        selected = selected[:max_files]
    keys = [input_key(document_path(raw_dir, d["id"]), raw_dir) for d in selected]
    clash = sorted({k for k in keys if keys.count(k) > 1})
    if clash:
        raise ValueError(
            f"documents share a stored file name (and so a marker): {', '.join(clash)}"
        )
    (raw_dir / SUBDIR).mkdir(parents=True, exist_ok=True)
    remove_stale_temps(raw_dir / SUBDIR)

    requests_made = 0
    for d, key in zip(selected, keys, strict=True):
        final = document_path(raw_dir, d["id"])
        updated = str(d.get("last_updated_date") or "")
        if not force and final.is_file() and _marker_updated(processed_root, key) == updated:
            result["skipped"].append(d["id"])
            continue
        tmp = final.with_name(f".{final.name}.{os.getpid()}.tmp")
        last_error = ""
        attempt = 0
        waited = False
        while attempt < ATTEMPTS:
            # The pause after a throttle is the delay: no second wait before the new request.
            if requests_made and delay > 0 and not waited:
                sleep(delay)
            waited = False
            requests_made += 1
            try:
                stats = download_to(
                    session,
                    d["document_url"],
                    f"document {d['id']}",
                    tmp,
                    _check_pdf,
                    max_bytes=max_bytes,
                )
            except KeyboardInterrupt:
                tmp.unlink(missing_ok=True)
                drop_success_marker(processed_root, MARKER_SOURCE, key)
                raise
            except Throttled as exc:
                tmp.unlink(missing_ok=True)
                try:
                    gate.on_throttle(exc, f"document {d['id']}")
                except RateLimited as stop_exc:
                    drop_success_marker(processed_root, MARKER_SOURCE, key)
                    mark_failed(
                        processed_root,
                        MARKER_SOURCE,
                        key,
                        error_class="RateLimited",
                        message=str(stop_exc),
                        exc=stop_exc,
                    )
                    raise
                waited = True
                continue  # the same document again; this try does not count
            except FeedVerificationError as exc:
                tmp.unlink(missing_ok=True)
                drop_success_marker(processed_root, MARKER_SOURCE, key)
                mark_failed(
                    processed_root,
                    MARKER_SOURCE,
                    key,
                    error_class="FeedVerificationError",
                    message=str(exc),
                    exc=exc,
                )
                raise
            except TooLarge as exc:
                # Over EPISTEME_EPAR_MAX_BYTES: the extract would not read it either. Not
                # retried; the other documents go on.
                tmp.unlink(missing_ok=True)
                last_error = str(exc)
                drop_success_marker(processed_root, MARKER_SOURCE, key)
                mark_failed(
                    processed_root,
                    MARKER_SOURCE,
                    key,
                    error_class="over_limit",
                    message=last_error,
                    stats={"max_bytes": max_bytes},
                )
                result["failed"][d["id"]] = last_error
                break
            except Retryable as exc:
                tmp.unlink(missing_ok=True)
                last_error = str(exc)
                attempt += 1
                _LOG.warning(
                    "document %s: attempt %d of %d failed: %s", d["id"], attempt, ATTEMPTS, exc
                )
                continue
            os.replace(tmp, final)
            mark_success(
                processed_root,
                MARKER_SOURCE,
                key,
                stats={**stats, "last_updated": updated, "document_id": d["id"]},
            )
            gate.ok()
            # A failed marker of an earlier try is stale.
            failed_marker_path(processed_root, MARKER_SOURCE, key).unlink(missing_ok=True)
            result["fetched"].append(d["id"])
            break
        else:
            drop_success_marker(processed_root, MARKER_SOURCE, key)
            mark_failed(
                processed_root,
                MARKER_SOURCE,
                key,
                error_class="DownloadIncomplete",
                message=last_error,
                stats={"attempts": ATTEMPTS},
            )
            result["failed"][d["id"]] = last_error
    return result


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Download the English EPAR PDFs.")
    p.add_argument("--output-dir", type=Path, default=None, help="default: <raw root>/ema")
    p.add_argument("--dry-run", action="store_true", help="list the documents; fetch nothing")
    p.add_argument("--force", action="store_true", help="fetch again even with a success marker")
    p.add_argument("--max-files", type=int, default=None, help="fetch at most N documents")
    p.add_argument("--reason", default=None, help="accepted for run_pipeline.sh; not used")
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_parser().parse_args(argv)
    out_dir = args.output_dir or (get_settings().raw_root / "ema")
    feed_path = out_dir / FEED_FILE
    if not feed_path.is_file():
        print(
            f"ema_epar: the feed file {feed_path} is missing; run the ema download first",
            file=sys.stderr,
        )
        return 1
    stats: dict = {}
    docs = load_documents(feed_path, stats=stats)
    print(
        f"ema_epar: {len(docs)} documents to fetch; "
        f"refused url={stats.get('refused_url', 0)} id={stats.get('refused_id', 0)}"
    )
    if not docs:
        print("ema_epar: the feed lists no document of the wanted types", file=sys.stderr)
        return 1
    if args.dry_run:
        for d in docs[: args.max_files]:
            print(f"dry-run: would fetch document {d['id']} -> {document_path(out_dir, d['id'])}")
        return 0
    # No lock: a download holds no database and not the heavy lock. Only the stop handlers,
    # the stop-request file and the parent watchdog apply (conn_ref returns None).
    with stage_guard(lambda: None, argv=argv) as g:
        try:
            result = fetch_documents(out_dir, docs, force=args.force, max_files=args.max_files)
        except (FeedVerificationError, RateLimited) as exc:
            print(f"ema_epar: {exc}", file=sys.stderr)
            return g.exit_code(3)
        print(
            f"ema_epar: fetched={len(result['fetched'])} skipped={len(result['skipped'])} "
            f"failed={len(result['failed'])} refused={result['refused']}"
        )
        for doc_id, reason in result["failed"].items():
            print(f"ema_epar: FAILED document {doc_id}: {reason}", file=sys.stderr)
        return g.exit_code(1 if result["failed"] else 0)


if __name__ == "__main__":
    sys.exit(main())
