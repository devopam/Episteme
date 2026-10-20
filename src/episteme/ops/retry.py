"""Error classification and retry for the processing stages (operating
standard section 6.2). A transient error is retried 3 times, after 30, 60 and
120 s; anything else, and a transient error after the last retry, is permanent.

Classification uses SQLSTATE codes and errno/winerror values, never message
text. ``57014 query_canceled`` is permanent: it is what a deliberate stop or an
orphan sweep produces. Only ``Exception`` subclasses are retried, so
KeyboardInterrupt and SystemExit always propagate.
"""

from __future__ import annotations

import errno
import logging
import time
from collections.abc import Callable, Sequence
from typing import TypeVar

import psycopg

_LOG = logging.getLogger(__name__)
T = TypeVar("T")

RETRY_WAITS: tuple[int, ...] = (30, 60, 120)

# Connection exceptions (08xxx), serialization/deadlock (40001, 40P01),
# lock not available (55P03), operator intervention except query_canceled
# (57P01-57P05), too many connections (53300).
_TRANSIENT_SQLSTATE_PREFIXES = ("08",)
_TRANSIENT_SQLSTATES = frozenset(
    {"40001", "40P01", "55P03", "57P01", "57P02", "57P03", "57P04", "57P05", "53300"}
)
_TRANSIENT_ERRNOS = frozenset(
    {
        errno.EIO,
        errno.ETIMEDOUT,
        errno.ECONNRESET,
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.EPIPE,
        errno.EAGAIN,
    }
)
# Windows: 21 device not ready, 64 network name no longer available,
# 121 semaphore timeout, 1117 I/O device error, 1231 network unreachable.
_TRANSIENT_WINERRORS = frozenset({21, 64, 121, 1117, 1231})
_PERMANENT_OSERRORS = (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError)


class TransientUnitError(Exception):
    """A unit failed with a transient error inside a worker. Carries text only,
    so it pickles across a process pool without losing its meaning."""

    def __init__(self, unit: str, cause: str) -> None:
        super().__init__(unit, cause)
        self.unit = unit
        self.cause = cause

    def __str__(self) -> str:
        return f"transient error in {self.unit}: {self.cause}"


def is_transient(exc: BaseException) -> bool:
    if not isinstance(exc, Exception):
        return False
    if isinstance(exc, TransientUnitError):
        return True
    if isinstance(exc, psycopg.Error):
        state = getattr(exc, "sqlstate", None)
        if state is None:
            # No SQLSTATE: a client-side connection failure (lost or refused).
            return isinstance(exc, psycopg.OperationalError)
        return state in _TRANSIENT_SQLSTATES or state.startswith(_TRANSIENT_SQLSTATE_PREFIXES)
    if isinstance(exc, _PERMANENT_OSERRORS):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, OSError):
        if getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS:
            return True
        return exc.errno in _TRANSIENT_ERRNOS
    return False


def with_retries(
    fn: Callable[[], T],
    *,
    waits: Sequence[float] = RETRY_WAITS,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            if attempt >= len(waits) or not is_transient(exc):
                raise
            wait = waits[attempt]
            attempt += 1
            if on_retry is not None:
                on_retry(attempt, exc, wait)
            else:
                _LOG.warning(
                    "transient error (retry %d/%d in %ss): %r", attempt, len(waits), wait, exc
                )
            sleep(wait)
