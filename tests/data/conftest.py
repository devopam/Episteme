import os

import pytest


@pytest.fixture
def pg_conn():
    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN not set")
    import psycopg

    # Guard against a misconfigured TEST_PG_DSN pointing at the real
    # `episteme` database: 7 `pg`-marked test files run
    # `DROP SCHEMA IF EXISTS episteme CASCADE` as their first act using this
    # fixture's connection. Refuse anything whose dbname isn't
    # episteme_test, rather than risk dropping the real schema.
    dbname = psycopg.conninfo.conninfo_to_dict(dsn).get("dbname")
    if dbname != "episteme_test":
        pytest.fail(
            f"TEST_PG_DSN does not target episteme_test (dbname={dbname!r}); "
            "refusing to run a pg test that DROPs SCHEMA against it"
        )

    conn = psycopg.connect(dsn, autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
