"""Copy the API settings from config into the SQL tables the lookup functions read.

``python -m episteme.reference.api_settings sync`` writes
``episteme_api_internal.source_subset`` (from ``config.LOOKUP_SOURCE_SUBSETS``) and
``episteme_api_internal.result_limit`` (from ``config.api_result_limits()``) in one
transaction: delete, then insert. Run it after migration 0015 and after a change to
either setting. It needs the owner (admin) connection.
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from episteme.config import LOOKUP_SOURCE_SUBSETS, ConfigError, api_result_limits, get_settings
from episteme.data.db import connection as db_connection
from episteme.data.db.guard import ProductionDatabaseGuardError, pool_kwargs


def sync(conn: psycopg.Connection) -> dict[str, int]:
    """Replace both settings tables in one transaction. Returns the row counts."""
    subsets = sorted(LOOKUP_SOURCE_SUBSETS.items())
    limits = sorted((f, d, m) for f, (d, m) in api_result_limits().items())
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM episteme_api_internal.source_subset")
        cur.executemany(
            "INSERT INTO episteme_api_internal.source_subset (source, subset) VALUES (%s, %s)",
            subsets,
        )
        cur.execute("DELETE FROM episteme_api_internal.result_limit")
        cur.executemany(
            "INSERT INTO episteme_api_internal.result_limit "
            "(family, default_limit, max_limit) VALUES (%s, %s, %s)",
            limits,
        )
    return {"source_subset": len(subsets), "result_limit": len(limits)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SQL lookup API settings")
    parser.add_argument("action", choices=["sync"])
    parser.parse_args(argv)
    try:
        settings = get_settings()
        dsn = db_connection.admin_dsn_from_settings()
        with psycopg.connect(dsn, autocommit=False, **pool_kwargs(settings)) as conn:
            counts = sync(conn)
    except (ProductionDatabaseGuardError, ConfigError, ValueError, psycopg.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"api_settings sync: {counts['source_subset']} sources, {counts['result_limit']} limits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
