"""Download the EMA open-data feeds (JSON reports and the Article 57 workbook).

One file at a time, with a configured delay (``EPISTEME_EMA_DELAY_SECONDS``) between
requests. Each file is written to a temporary name and renamed when it is complete
and checked, so a stored file is always whole. Each feed has a success or failed
marker, keyed on ``input_key`` of the stored file (``<feed name>.json`` or
``.xlsx``), so a re-run skips finished feeds.

EMA answers automated requests with a human-verification page under some
conditions (status 200 and HTML). That is never data and never retried: the stage
writes a failed marker and stops with ``FeedVerificationError``. A download that is
cut short (curl error 18 was seen on the non-EPAR documents feed) is tried once
more and then recorded as failed; the other feeds still run.

A 429 or 503 is a throttle, not a failure of the file: the whole run pauses (``Retry-After``
when EMA sends it, else a backoff that doubles per throttle in a row), and the same file is
asked for again without using one of its attempts. After
``EPISTEME_EMA_MAX_THROTTLES`` throttles in a row the stage records a failed marker and
stops with ``RateLimited`` (exit 3); a re-run resumes.

A feed whose row count differs from ``meta.total_records`` is stored with both
numbers in its marker. The herbal feed does this; the loader decides.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import ijson
import requests

from episteme.config import get_settings, source_url
from episteme.data.checkpoint_markers import (
    input_key,
    is_success,
    mark_failed,
    mark_success,
)
from episteme.data.regulatory.feeds import Feed, load_feeds
from episteme.data.regulatory.parsers import ema_total
from episteme.ops.stop import stage_guard

_LOG = logging.getLogger(__name__)

MARKER_SOURCE = "ema_download"
ATTEMPTS = 2  # the first try and one retry
_EXT = {"ema_json": ".json", "article57_xlsx": ".xlsx"}
_CONNECT_READ_TIMEOUT = (30, 300)
_CHUNK = 1 << 20


class FeedVerificationError(RuntimeError):
    """EMA answered with its human-verification page (or any HTML) instead of data."""


class Retryable(RuntimeError):
    """A failed or incomplete download that one more try may fix."""


class Throttled(RuntimeError):
    """EMA answered 429 or 503. ``retry_after`` is the wait the ``Retry-After`` header
    asks for in seconds, or None when it is missing or unreadable."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class RateLimited(RuntimeError):
    """Too many throttles in a row: the stage stops (exit 3) and keeps what it fetched."""


RATE_LIMIT_MESSAGE = "EMA rate limit: run again later; fetched files are kept"
_THROTTLE_STATUS = (429, 503)
WAIT_SLICE_S = 1.0
MIN_WAIT_S = 1.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_retry_after(value: Any) -> float | None:
    """Seconds from a ``Retry-After`` value: a number of seconds, or an HTTP date. None when
    the value is missing, unreadable, or a date that has passed."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.isascii() and text.isdigit():
        return float(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    secs = (when - _utcnow()).total_seconds()
    return secs if secs > 0 else None


class ThrottleGate:
    """The pause rule of one run. ``on_throttle`` waits (through ``sleep``) or, at the
    ``max_throttles``-th throttle in a row, raises ``RateLimited`` without a wait; ``ok``
    resets the count. The wait is ``Retry-After`` (capped), else ``backoff_s`` doubled per
    throttle in a row (capped)."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], None],
        backoff_s: float | None = None,
        max_backoff_s: float | None = None,
        max_throttles: int | None = None,
    ):
        settings = get_settings()
        self.sleep = sleep
        self.backoff_s = settings.ema_backoff_seconds if backoff_s is None else backoff_s
        self.max_backoff_s = (
            settings.ema_max_backoff_seconds if max_backoff_s is None else max_backoff_s
        )
        self.max_throttles = settings.ema_max_throttles if max_throttles is None else max_throttles
        self.count = 0

    def ok(self) -> None:
        self.count = 0

    def on_throttle(self, exc: Throttled, label: str) -> float:
        self.count += 1
        if self.count >= self.max_throttles:
            raise RateLimited(
                f"{RATE_LIMIT_MESSAGE} ({label}: {exc}; {self.count} throttles in a row)"
            ) from exc
        if exc.retry_after is not None:
            wait = min(exc.retry_after, self.max_backoff_s)
        else:
            wait = min(self.backoff_s * 2 ** (self.count - 1), self.max_backoff_s)
        wait = max(wait, MIN_WAIT_S)  # a 0 from Retry-After or a setting still waits
        _LOG.warning(
            "%s: %s; throttle %d of %d, waiting %.0f s",
            label,
            exc,
            self.count,
            self.max_throttles,
            wait,
        )
        # Slices, so a pending stop (an interrupt that cannot wake one long sleep) raises
        # between two slices.
        left = wait
        while left > 0:
            piece = min(WAIT_SLICE_S, left)
            self.sleep(piece)
            left -= piece
        return wait


class TooLarge(RuntimeError):
    """The body is over the byte cap of ``download_to``. Not retried: it can never fit."""


def ema_feeds(feeds: dict[str, Feed]) -> list[Feed]:
    """The feeds this stage downloads: the ones that have a file of their own on EMA's site."""
    return [f for f in feeds.values() if f.format in _EXT]


def _looks_like_html(head: bytes) -> bool:
    return head.lstrip(b"\xef\xbb\xbf \t\r\n")[:1] == b"<"


def _check_complete(tmp: Path, feed: Feed) -> dict[str, Any]:
    """Prove the stored bytes are a whole file of the feed's format; return stats."""
    if feed.format == "article57_xlsx":
        try:
            with zipfile.ZipFile(tmp) as zf:
                if "xl/workbook.xml" not in zf.namelist():
                    raise Retryable("not a workbook")
        except zipfile.BadZipFile as exc:
            raise Retryable(f"truncated workbook: {exc}") from exc
        return {}
    rows = 0
    try:
        with open(tmp, "rb") as fh:
            for _ in ijson.items(fh, "data.item", use_float=True):
                rows += 1
    except ijson.JSONError as exc:
        raise Retryable(f"truncated or invalid JSON: {exc}") from exc
    total = ema_total(tmp)
    if total is None:
        raise Retryable("no meta.total_records")
    if rows != total:
        _LOG.warning("%s: %d rows but meta.total_records = %d", feed.name, rows, total)
    return {"rows": rows, "total_records": total}


def download_to(
    session: Any,
    url: str,
    label: str,
    tmp: Path,
    check: Callable[[Path], dict[str, Any]],
    *,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """One request into ``tmp``, then ``check(tmp)`` proves the bytes are a whole file
    and returns stats. Raises ``FeedVerificationError`` (status 200 with HTML, or a body
    that starts like HTML) or ``Retryable``. ``check`` may raise either too; this is the
    one place the EMA feeds and the EPAR documents share their transfer rules.

    With ``max_bytes``, a body over the cap raises ``TooLarge``: at once when
    ``Content-Length`` says so, else as soon as the stream passes the cap (the caller
    deletes ``tmp``). The EMA feeds pass no cap."""
    try:
        resp = session.get(url, stream=True, timeout=_CONNECT_READ_TIMEOUT)
    except (requests.RequestException, OSError) as exc:
        raise Retryable(f"request failed: {exc}") from exc
    try:
        ctype = str(resp.headers.get("Content-Type", "")).lower()
        if resp.status_code in _THROTTLE_STATUS:
            raise Throttled(
                f"HTTP {resp.status_code}", parse_retry_after(resp.headers.get("Retry-After"))
            )
        if resp.status_code != 200:
            # An HTML error page (404, 5xx) is an ordinary error, not the verification page.
            raise Retryable(f"HTTP {resp.status_code}")
        if "html" in ctype:
            raise FeedVerificationError(
                f"{label}: EMA answered status 200 with HTML, not data: "
                "its human-verification page; the stage stops"
            )
        declared_len = str(resp.headers.get("Content-Length") or "")
        if max_bytes is not None and declared_len.isdigit() and int(declared_len) > max_bytes:
            raise TooLarge(f"{label}: {declared_len} bytes declared, over the cap of {max_bytes}")
        size = 0
        first = True
        with open(tmp, "wb") as out:
            try:
                for chunk in resp.iter_content(chunk_size=_CHUNK):
                    if not chunk:
                        continue
                    if first:
                        first = False
                        if _looks_like_html(chunk):
                            raise FeedVerificationError(
                                f"{label}: EMA answered with an HTML body, not data: "
                                "its human-verification page; the stage stops"
                            )
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise TooLarge(f"{label}: over the cap of {max_bytes} bytes; stopped")
                    out.write(chunk)
            except (requests.RequestException, OSError) as exc:
                raise Retryable(f"transfer failed after {size} bytes: {exc}") from exc
            out.flush()
            os.fsync(out.fileno())
        declared = resp.headers.get("Content-Length")
        # With Content-Encoding set, requests writes the decoded bytes and Content-Length
        # counts the encoded ones: the two cannot be compared. The parse and zip checks
        # below then prove the file is whole.
        encoded = bool(str(resp.headers.get("Content-Encoding", "")).strip())
        if (
            not encoded
            and declared is not None
            and str(declared).isdigit()
            and int(declared) != size
        ):
            raise Retryable(f"truncated: {size} of {declared} bytes")
        if size == 0:
            raise Retryable("empty response")
        stats = check(tmp)
        stats["bytes"] = size
        return stats
    finally:
        close = getattr(resp, "close", None)
        if close:
            close()


def _download_once(session: Any, url: str, feed: Feed, tmp: Path) -> dict[str, Any]:
    """One feed request into ``tmp``. Raises ``FeedVerificationError`` or ``Retryable``."""
    return download_to(session, url, feed.name, tmp, lambda p: _check_complete(p, feed))


def fetch_ema_feeds(
    raw_dir: Path,
    feeds: dict[str, Feed],
    *,
    session: Any = None,
    processed_root: Path | None = None,
    delay_s: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    force: bool = False,
    max_files: int | None = None,
    backoff_s: float | None = None,
    max_backoff_s: float | None = None,
    max_throttles: int | None = None,
) -> dict:
    """Download every EMA feed in ``feeds`` into ``raw_dir``.

    Returns ``{"fetched": [...], "skipped": [...], "failed": {name: reason}}``.
    Raises ``FeedVerificationError`` (after its failed marker) when EMA answers with
    its verification page, and ``RateLimited`` (after its failed marker) after too many
    throttles in a row. ``session`` and ``sleep`` are seams for tests.
    """
    raw_dir = Path(raw_dir)
    settings = get_settings()
    processed_root = Path(processed_root) if processed_root else settings.processed_root
    delay = settings.ema_delay_seconds if delay_s is None else delay_s
    session = session or requests.Session()
    gate = ThrottleGate(
        sleep=sleep, backoff_s=backoff_s, max_backoff_s=max_backoff_s, max_throttles=max_throttles
    )
    raw_dir.mkdir(parents=True, exist_ok=True)

    result: dict = {"fetched": [], "skipped": [], "failed": {}}
    requests_made = 0
    selected = ema_feeds(feeds)
    if max_files is not None:
        selected = selected[:max_files]
    keys = [input_key(raw_dir / f"{f.name}{_EXT[f.format]}", raw_dir) for f in selected]
    clash = sorted({k for k in keys if keys.count(k) > 1})
    if clash:
        raise ValueError(f"feeds share a stored file name (and so a marker): {', '.join(clash)}")
    for feed in selected:
        final = raw_dir / f"{feed.name}{_EXT[feed.format]}"
        key = input_key(final, raw_dir)
        if not force and final.is_file() and is_success(processed_root, MARKER_SOURCE, key):
            result["skipped"].append(feed.name)
            continue
        url = source_url(feed.url_key)
        tmp = raw_dir / f".{final.name}.{os.getpid()}.tmp"
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
                stats = _download_once(session, url, feed, tmp)
            except KeyboardInterrupt:
                tmp.unlink(missing_ok=True)
                raise
            except Throttled as exc:
                tmp.unlink(missing_ok=True)
                try:
                    gate.on_throttle(exc, feed.name)
                except RateLimited as stop_exc:
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
                continue  # the same feed again; this try does not count
            except FeedVerificationError as exc:
                tmp.unlink(missing_ok=True)
                mark_failed(
                    processed_root,
                    MARKER_SOURCE,
                    key,
                    error_class="FeedVerificationError",
                    message=str(exc),
                    exc=exc,
                )
                raise
            except Retryable as exc:
                tmp.unlink(missing_ok=True)
                last_error = str(exc)
                attempt += 1
                _LOG.warning("%s: attempt %d of %d failed: %s", feed.name, attempt, ATTEMPTS, exc)
                continue
            gate.ok()
            os.replace(tmp, final)
            mark_success(
                processed_root, MARKER_SOURCE, key, stats={**stats, "url_key": feed.url_key}
            )
            result["fetched"].append(feed.name)
            break
        else:
            mark_failed(
                processed_root,
                MARKER_SOURCE,
                key,
                error_class="DownloadIncomplete",
                message=last_error,
                stats={"attempts": ATTEMPTS},
            )
            result["failed"][feed.name] = last_error
    return result


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Download the EMA open-data feeds.")
    p.add_argument("--output-dir", type=Path, default=None, help="default: <raw root>/ema")
    p.add_argument("--dry-run", action="store_true", help="list the feeds; fetch nothing")
    p.add_argument("--force", action="store_true", help="fetch again even with a success marker")
    p.add_argument("--max-files", type=int, default=None, help="fetch at most N feeds")
    p.add_argument("--reason", default=None, help="accepted for run_pipeline.sh; not used")
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_parser().parse_args(argv)
    feeds = load_feeds()
    out_dir = args.output_dir or (get_settings().raw_root / "ema")
    if args.dry_run:
        for feed in ema_feeds(feeds)[: args.max_files]:
            print(
                f"dry-run: would fetch {feed.url_key} -> {out_dir}/{feed.name}{_EXT[feed.format]}"
            )
        return 0
    # No lock: a download holds no database and not the heavy lock. Only the stop handlers,
    # the stop-request file and the parent watchdog apply (conn_ref returns None).
    with stage_guard(lambda: None, argv=argv) as g:
        try:
            result = fetch_ema_feeds(out_dir, feeds, force=args.force, max_files=args.max_files)
        except (FeedVerificationError, RateLimited) as exc:
            print(f"ema: {exc}", file=sys.stderr)
            return g.exit_code(3)
        print(
            f"ema: fetched={len(result['fetched'])} skipped={len(result['skipped'])} "
            f"failed={len(result['failed'])}"
        )
        for name, reason in result["failed"].items():
            print(f"ema: FAILED {name}: {reason}", file=sys.stderr)
        return g.exit_code(1 if result["failed"] else 0)


if __name__ == "__main__":
    sys.exit(main())
