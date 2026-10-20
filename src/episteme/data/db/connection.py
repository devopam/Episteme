"""PostgreSQL connection pool for the episteme pipeline.

DSN is built from episteme.config.get_settings() — config.py stays the sole
os.environ reader. The pipeline connects as the non-superuser episteme_app
role (see scripts/data/db/init_database.sh).
"""

from __future__ import annotations

import atexit
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from multiprocessing import util as mp_util

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

from episteme.config import ConfigError, get_settings
from episteme.data.db.guard import check_database_allowed, pool_kwargs

_POOL: ConnectionPool | None = None
_ATEXIT_PID: int | None = None  # process that already registered the close hooks
_CLOSED_POOL: ConnectionPool | None = None  # pool the exit hooks already closed
_POOL_LOCK = threading.Lock()
_CLOSE_TIMEOUT_SEC = 2.0


def dsn_from_settings() -> str:
    settings = get_settings()
    check_database_allowed(settings)
    return settings.pg_dsn()


SYS_ADMIN_ROLE = "episteme_sys_admin"

_APP_PREFIX = "episteme"
_APP_NAME_MAX = 63  # PostgreSQL truncates application_name at NAMEDATALEN - 1 bytes


def app_name(settings, suffix: str = "") -> str:
    """``episteme:<run_id>:<pid>`` for ``application_name``, at most 63 bytes.
    The run id is shortened first, so ``:<pid>`` always survives; the orphan
    sweep reads the client PID from the last field. ``adhoc`` stands in for a
    standalone CLI (no run id). The PID is read at call time. ``suffix`` (for
    example ``:lock``) goes after the PID; it takes room from the run id."""
    run = settings.run_id or "adhoc"
    head, tail = f"{_APP_PREFIX}:", f":{os.getpid()}{suffix}"
    room = _APP_NAME_MAX - len(head.encode()) - len(tail.encode())
    run = run.encode()[:room].decode("utf-8", errors="ignore")
    return f"{head}{run}{tail}"


def connect_kwargs(settings) -> dict[str, str]:
    """``pool_kwargs`` plus the ``application_name`` tag."""
    return {**pool_kwargs(settings), "application_name": app_name(settings)}


def admin_dsn(settings) -> str:
    """DSN for the table owner, ``episteme_sys_admin``: dropping and creating
    the indexes of ``episteme.articles`` needs it, and ``episteme_app`` cannot.
    Runs the same DB-mode guard as ``dsn_from_settings`` first. ``make_conninfo``
    quotes the password, so special characters are safe."""
    check_database_allowed(settings)
    if not settings.sys_admin_password:
        raise ConfigError(
            "EPISTEME_SYS_ADMIN_PASSWORD is not set (needed to connect as episteme_sys_admin)"
        )
    return make_conninfo(
        host=settings.pg_host,
        port=settings.pg_port,
        dbname=settings.pg_database,
        user=SYS_ADMIN_ROLE,
        password=settings.sys_admin_password,
        application_name=app_name(settings),
    )


def admin_dsn_from_settings() -> str:
    return admin_dsn(get_settings())


def _close_pool_at_exit() -> None:
    """Close the pool at process exit, so its worker threads stop quietly
    ("couldn't stop thread ... within 5.0 seconds"). Never raises; a second
    call for the same pool does nothing (two hooks are registered)."""
    global _CLOSED_POOL
    try:
        pool = _POOL
        if pool is None or pool is _CLOSED_POOL:
            return
        _CLOSED_POOL = pool
        pool.close(timeout=_CLOSE_TIMEOUT_SEC)
    except Exception:  # noqa: BLE001 - exit hook must not fail
        pass


def get_pool() -> ConnectionPool:
    global _POOL, _ATEXIT_PID
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                # create the pool in the process that uses it; a pool inherited
                # through fork carries the parent's PID in application_name
                _POOL = ConnectionPool(
                    dsn_from_settings(),
                    kwargs=connect_kwargs(get_settings()),
                    min_size=1,
                    max_size=8,
                    open=True,
                )
                if _ATEXIT_PID != os.getpid():
                    # atexit covers sys.exit (Windows spawn workers); Finalize
                    # covers POSIX fork children, which leave through os._exit.
                    atexit.register(_close_pool_at_exit)
                    mp_util.Finalize(None, _close_pool_at_exit, exitpriority=10)
                    _ATEXIT_PID = os.getpid()
    return _POOL


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    with get_pool().connection() as conn:
        yield conn
