"""Stop handling: turn SIGTERM/SIGBREAK into KeyboardInterrupt and cancel the
running statement when the parent process dies.

A hard kill of the runner on Windows once left a 3-hour orphan DELETE running
in PostgreSQL. A stage wraps its main in ``stage_guard`` so a stop or a dead
parent cancels the statement instead of leaving it behind.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import psutil

_LOG = logging.getLogger(__name__)

EXIT_INT = 130
EXIT_TERM = 143

_TERM_SEEN = False


def _on_term(signum: int, frame: Any) -> None:
    global _TERM_SEEN
    _TERM_SEEN = True
    signal.default_int_handler(signum, frame)


def install_stop_handlers() -> None:
    """SIGTERM (and SIGBREAK on Windows) raise ``KeyboardInterrupt`` in the
    main thread. Safe to call more than once."""
    names = ("SIGTERM", "SIGBREAK")
    for name in names:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _on_term)
        except ValueError:  # not the main thread
            _LOG.debug("cannot install %s handler outside the main thread", name)


def start_parent_watchdog(
    cancel: Callable[[], None],
    *,
    interval: float = 10.0,
    parent_pid: int | None = None,
) -> threading.Thread:
    """Start a daemon thread that cancels the running statement and exits the
    process (code 143) when the parent process is gone."""
    ppid = parent_pid or os.getppid()

    def _watch() -> None:
        while psutil.pid_exists(ppid):
            time.sleep(interval)
        try:
            cancel()
        except Exception:  # noqa: BLE001 - the process exits next
            _LOG.exception("cancel failed after parent %s exited", ppid)
        _LOG.error("parent process %s is gone; exiting", ppid)
        os._exit(EXIT_TERM)

    t = threading.Thread(target=_watch, name="parent-watchdog", daemon=True)
    t.start()
    return t


@contextmanager
def stage_guard(conn_ref: Callable[[], Any]) -> Iterator[None]:
    """Stage-main wrapper: install the handlers, start the watchdog, and map a
    stop to ``SystemExit`` 130 (INT) or 143 (TERM)."""

    def _cancel() -> None:
        c = conn_ref()
        if c is not None:
            c.cancel_safe()

    install_stop_handlers()
    start_parent_watchdog(_cancel)
    try:
        yield
    except KeyboardInterrupt:
        try:
            _cancel()
        except Exception:  # noqa: BLE001 - best effort; the exit follows
            _LOG.exception("cancel failed on stop")
        raise SystemExit(EXIT_TERM if _TERM_SEEN else EXIT_INT) from None
