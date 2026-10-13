"""PostgreSQL connection pool for the episteme pipeline.

DSN is built from episteme.config.get_settings() — config.py stays the sole
os.environ reader. The pipeline connects as the non-superuser episteme_app
role (see scripts/data/db/init_database.sh).
"""

from __future__ import annotations

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
    )


def admin_dsn_from_settings() -> str:
    return admin_dsn(get_settings())


def get_pool() -> ConnectionPool:
    global _POOL
    if _POOL is None:
        _POOL = ConnectionPool(
            dsn_from_settings(),
            kwargs=pool_kwargs(get_settings()),
            min_size=1,
            max_size=8,
            open=True,
        )
    return _POOL


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    with get_pool().connection() as conn:
        yield conn
