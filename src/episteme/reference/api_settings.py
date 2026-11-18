"""Copy the API settings from config into the SQL tables the lookup functions read.

``python -m episteme.reference.api_settings sync`` writes
``episteme_api_internal.source_subset`` (from ``config.LOOKUP_SOURCE_SUBSETS``) and
``episteme_api_internal.result_limit`` (from ``config.api_result_limits()``) in one
transaction: delete, then insert. It then applies ``grants/0015_api_tables.sql`` (the
reads of the API owner role: USAGE on ``episteme``, SELECT on exactly the relations the
functions read, the latest AACT release schema; a no-op before the grants file created
the role), and sets the ``statement_timeout`` of the role
``episteme_api_reader`` (``config.api_reader_statement_timeout_s``), when the role exists:
a second guard after the row caps (the grants file ``grants/0015_api_roles.sql`` creates
the role and gives ``episteme_sys_admin`` ADMIN on it). Run it after migration 0015, after
the grants file, after each AACT load, after a rebuild of a table the functions read and
after a change to any of these settings. It needs the owner (admin) connection.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg
from psycopg import sql

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


READER_ROLE = "episteme_api_reader"
OWNER_GRANTS = Path(__file__).resolve().parents[1] / "data/db/grants/0015_api_tables.sql"


def apply_owner_grants(conn: psycopg.Connection) -> bool:
    """Apply ``grants/0015_api_tables.sql`` in one transaction (the connected role must own
    the tables, or be a superuser). Returns whether the owner role exists; without it the
    file does nothing."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT to_regrole('episteme_api_owner') IS NOT NULL")
        exists = bool(cur.fetchone()[0])
        if exists:
            cur.execute(OWNER_GRANTS.read_text(encoding="utf-8"))
    return exists


def apply_reader_settings(conn: psycopg.Connection, timeout_s: int) -> str | None:
    """Set the reader role's ``statement_timeout`` to ``timeout_s`` seconds (0 = none).
    Returns the value set, or ``None`` when the role does not exist (grants file not yet
    applied). The setting is per role, so it holds in every database of the server; the
    reader can still lower or raise it with ``SET`` in its own session (MCPg restricted
    mode is the wall there)."""
    if timeout_s < 0:
        raise ValueError(f"statement timeout must be 0 or more seconds, got {timeout_s}")
    value = f"{timeout_s}s" if timeout_s else "0"
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT to_regrole(%s) IS NOT NULL", (READER_ROLE,))
        if not cur.fetchone()[0]:
            return None
        cur.execute(
            sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(
                sql.Identifier(READER_ROLE), sql.Literal(value)
            )
        )
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SQL lookup API settings")
    parser.add_argument("action", choices=["sync"])
    parser.parse_args(argv)
    try:
        settings = get_settings()
        dsn = db_connection.admin_dsn_from_settings()
        with psycopg.connect(dsn, autocommit=False, **pool_kwargs(settings)) as conn:
            counts = sync(conn)
            owner = apply_owner_grants(conn)
            timeout = apply_reader_settings(conn, settings.api_reader_statement_timeout_s)
    except (ProductionDatabaseGuardError, ConfigError, ValueError, psycopg.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"api_settings sync: {counts['source_subset']} sources, {counts['result_limit']} limits")
    if not owner:
        print("api_settings sync: role episteme_api_owner does not exist; apply the grants file")
    if timeout is None:
        print(f"api_settings sync: role {READER_ROLE} does not exist; apply the grants file")
    else:
        print(f"api_settings sync: {READER_ROLE} statement_timeout = {timeout}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
