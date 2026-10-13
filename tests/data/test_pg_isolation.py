"""pg tests reach episteme_test only: pg_conn pins every settings-based connection.

Regression for the 2026-10-13 incident: a pg test calling load_articles.main()
followed .env to the production database."""

from __future__ import annotations

import pytest
from psycopg.conninfo import conninfo_to_dict

pytestmark = pytest.mark.pg


def test_settings_follow_test_pg_dsn(pg_conn):
    import episteme.config as cfg
    import episteme.data.db.connection as conn_mod

    s = cfg.get_settings()
    assert s.pg_database == "episteme_test"
    assert s.db_mode == "restricted"
    assert s.production_database == "episteme"
    # connection.py may hold a get_settings bound before a reload elsewhere
    assert conn_mod.get_settings().pg_database == "episteme_test"
    assert conninfo_to_dict(conn_mod.dsn_from_settings())["dbname"] == "episteme_test"


def test_settings_connection_lands_in_episteme_test(pg_conn):
    import episteme.data.db.connection as conn_mod

    try:
        with conn_mod.connection() as c, c.cursor() as cur:
            cur.execute("select current_database()")
            assert cur.fetchone()[0] == "episteme_test"
    finally:
        if conn_mod._POOL is not None:
            conn_mod._POOL.close()


def test_production_name_is_refused_by_the_guard(pg_conn, monkeypatch):
    import episteme.config as cfg
    from episteme.data.db import connection as conn_mod
    from episteme.data.db.guard import ProductionDatabaseGuardError

    monkeypatch.setenv("PGDATABASE", "episteme")
    cfg.get_settings.cache_clear()
    conn_mod.get_settings.cache_clear()
    with pytest.raises(ProductionDatabaseGuardError):
        conn_mod.dsn_from_settings()
