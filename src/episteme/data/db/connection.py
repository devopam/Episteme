"""PostgreSQL connection pool for the episteme pipeline.

DSN is built from episteme.config.get_settings() — config.py stays the sole
os.environ reader. The pipeline connects as the non-superuser episteme_app
role (see scripts/data/db/init_database.sh).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg_pool import ConnectionPool

from episteme.config import get_settings
from episteme.data.db.guard import check_database_allowed, pool_kwargs

_POOL: ConnectionPool | None = None


def dsn_from_settings() -> str:
    settings = get_settings()
    check_database_allowed(settings)
    return settings.pg_dsn()


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
