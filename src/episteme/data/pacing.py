"""Request spacing that survives a restart, one small file per host.

EMA answers HTTP 429 when a new download run starts soon after the previous run's last request.
Both EMA downloaders therefore write the time of every request to
``<raw root>/_pacing/<host>.json`` and, before the first request of a run, wait out what is left
of the minimum interval. The file is a courtesy: an unreadable file or a failed write is logged
as a warning and never stops a run.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from episteme.config import get_settings

_LOG = logging.getLogger(__name__)
_HOST = re.compile(r"^[A-Za-z0-9.-]+$")


def default_pacing_dir() -> Path:
    return get_settings().raw_root / "_pacing"


def pacing_path(host: str, pacing_dir: Path | None = None) -> Path:
    """The pacing file of ``host``. ``pacing_dir`` is the test seam."""
    if not _HOST.fullmatch(host):
        raise ValueError(f"not a host name: {host!r}")
    return Path(pacing_dir or default_pacing_dir()) / f"{host}.json"


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def read_last_request(path: Path) -> datetime | None:
    """The time of the last recorded request (UTC), or None. A missing file is silent; an
    unreadable or corrupt file logs one warning."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        _LOG.warning("pacing file %s is unreadable (%s); ignored", path, exc)
        return None
    try:
        return _utc(datetime.fromisoformat(json.loads(text)["last_request_at"]))
    except (ValueError, KeyError, TypeError) as exc:
        _LOG.warning("pacing file %s is corrupt (%s); ignored", path, exc)
        return None


def record_request(path: Path, at: datetime) -> None:
    """Write ``at`` as the last request time (temp file, then replace). A write failure logs
    a warning: spacing is best effort and must not stop a run."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    body = {"host": path.stem, "last_request_at": _utc(at).isoformat()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(body), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        _LOG.warning("pacing file %s could not be written (%s); run goes on", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def wait_seconds(
    last: datetime | None, now: datetime, min_interval_s: float, cap_s: float | None = None
) -> float:
    """Seconds still to wait: ``min_interval_s`` minus the time since ``last``, never below 0
    and never above ``cap_s`` (default ``min_interval_s``; this covers a ``last`` in the
    future). A ``last`` set ahead of the clock carries a ``Retry-After`` longer than the
    interval, so a downloader passes a larger cap."""
    if last is None:
        return 0.0
    cap = min_interval_s if cap_s is None else max(cap_s, min_interval_s)
    return min(cap, max(0.0, min_interval_s - (_utc(now) - last).total_seconds()))


def foreign_wait(
    path: Path,
    own_last: datetime | None,
    now: datetime,
    min_interval_s: float,
    cap_s: float | None = None,
) -> float:
    """``wait_seconds`` for the file, but 0 when the file still holds the time this run wrote
    itself (the run's own in-memory spacing covers that case)."""
    last = read_last_request(path)
    if last is not None and own_last is not None and last == _utc(own_last):
        return 0.0
    return wait_seconds(last, now, min_interval_s, cap_s)
