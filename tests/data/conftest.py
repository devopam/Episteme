import os

import pytest


@pytest.fixture
def pg_conn():
    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN not set")
    import psycopg

    conn = psycopg.connect(dsn, autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
