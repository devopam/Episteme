import os

import pytest


def _reset_settings_and_pool(monkeypatch) -> None:
    """Drop cached Settings everywhere they may be bound, and the pool.

    Other pg tests ``importlib.reload(episteme.config)``, which rebinds
    ``episteme.config.get_settings`` while ``connection.py`` keeps the
    object it imported, so clear both. Drop the pool so it is rebuilt
    from the pinned settings."""
    import episteme.config as cfg
    import episteme.data.db.connection as conn_mod

    cfg.get_settings.cache_clear()
    conn_mod.get_settings.cache_clear()
    monkeypatch.setattr(conn_mod, "_POOL", None)


@pytest.fixture
def pg_conn(monkeypatch):
    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN not set")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    # Guard against a misconfigured TEST_PG_DSN pointing at the real
    # `episteme` database: 7 `pg`-marked test files run
    # `DROP SCHEMA IF EXISTS episteme CASCADE` as their first act using this
    # fixture's connection. Refuse anything whose dbname isn't
    # episteme_test, rather than risk dropping the real schema.
    parts = conninfo_to_dict(dsn)
    dbname = parts.get("dbname")
    if dbname != "episteme_test":
        pytest.fail(
            f"TEST_PG_DSN does not target episteme_test (dbname={dbname!r}); "
            "refusing to run a pg test that DROPs SCHEMA against it"
        )

    # Pin every settings-based connection (pool, connection(), CLIs such as
    # load_articles.main / retire.main) to the same database as this fixture.
    # Without this they follow .env -- which on a developer machine may name
    # the production database (incident 2026-10-13: a pg test loaded fixture
    # rows into production). restricted mode + the production name is the
    # second wall: the guard refuses `episteme` before any connection.
    monkeypatch.setenv("PGDATABASE", dbname)
    for key, env in (
        ("host", "PGHOST"),
        ("port", "PGPORT"),
        ("user", "PGUSER"),
        ("password", "PGPASSWORD"),
    ):
        if parts.get(key):
            monkeypatch.setenv(env, str(parts[key]))
    monkeypatch.setenv("EPISTEME_DB_MODE", "restricted")
    monkeypatch.setenv("EPISTEME_PRODUCTION_DATABASE", "episteme")
    monkeypatch.setenv("EPISTEME_DB_TARGET", "primary")
    monkeypatch.delenv("PGDATABASE_SECONDARY", raising=False)
    _reset_settings_and_pool(monkeypatch)

    conn = psycopg.connect(dsn, autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
