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

_POOL: ConnectionPool | None = None


def dsn_from_settings() -> str:
    s = get_settings()
    return (
        f"host={s.pg_host} port={s.pg_port} dbname={s.pg_database} "
        f"user={s.pg_user} password={s.pg_password}"
    )


def get_pool() -> ConnectionPool:
    global _POOL
    if _POOL is None:
        _POOL = ConnectionPool(dsn_from_settings(), min_size=1, max_size=8, open=True)
    return _POOL


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    with get_pool().connection() as conn:
        yield conn
