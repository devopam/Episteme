"""PostgreSQL connection pool for the episteme pipeline.

DSN is built from episteme.config.get_settings() — config.py stays the sole
os.environ reader. The pipeline connects as the non-superuser episteme_app
role (see scripts/data/db/init_database.sh).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

from episteme.config import ConfigError, get_settings
from episteme.data.db.guard import check_database_allowed, pool_kwargs

_POOL: ConnectionPool | None = None


def dsn_from_settings() -> str:
    settings = get_settings()
    check_database_allowed(settings)
    return settings.pg_dsn()


SYS_ADMIN_ROLE = "episteme_sys_admin"

_APP_PREFIX = "episteme"
_APP_NAME_MAX = 63  # PostgreSQL truncates application_name at NAMEDATALEN - 1 bytes


def app_name(settings) -> str:
    """``episteme:<run_id>:<pid>`` for ``application_name``, at most 63 bytes.
    The run id is shortened first, so ``:<pid>`` always survives; the orphan
    sweep reads the client PID from the last field. ``adhoc`` stands in for a
    standalone CLI (no run id). The PID is read at call time."""
    run = settings.run_id or "adhoc"
    head, tail = f"{_APP_PREFIX}:", f":{os.getpid()}"
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


def get_pool() -> ConnectionPool:
    global _POOL
    if _POOL is None:
        _POOL = ConnectionPool(
            dsn_from_settings(),
            kwargs=connect_kwargs(get_settings()),
            min_size=1,
            max_size=8,
            open=True,
        )
    return _POOL


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    with get_pool().connection() as conn:
        yield conn
