"""pg fixtures of the PBRER lookup tests. Not autouse: ``test_resolve`` and the
pure tests do not trigger the schema rebuild."""

from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from episteme.reference import lookups

from ._pbrer_db import TEST_AACT_SCHEMA, add_substances, build_schema, clear


@pytest.fixture(scope="module")
def ref_schema():
    """Build the schema once per module, on its own connection (``pg_conn`` is
    function-scoped). Same guard as ``pg_conn``: only ``episteme_test``."""
    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN not set")
    if conninfo_to_dict(dsn).get("dbname") != "episteme_test":
        pytest.fail("TEST_PG_DSN does not target episteme_test; refusing to DROP SCHEMA")
    conn = psycopg.connect(dsn, autocommit=False)
    try:
        build_schema(conn)
    finally:
        conn.close()


@pytest.fixture
def ref_db(ref_schema, pg_conn, clean_substance, monkeypatch):
    """A clean schema with three substances; AACT reads the test schema."""
    monkeypatch.setattr(lookups, "AACT_SCHEMA", TEST_AACT_SCHEMA)
    clear(pg_conn)
    add_substances(pg_conn)
    yield pg_conn
    pg_conn.rollback()
    clear(pg_conn)
