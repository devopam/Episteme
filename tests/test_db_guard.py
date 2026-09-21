from dataclasses import replace

import pytest

import episteme.config as _cfg  # attribute access at call time: other tests reload config
from episteme.config import get_settings
from episteme.data.db.guard import (
    DB_MODES,
    ProductionDatabaseGuardError,
    check_database_allowed,
    pool_kwargs,
)


def _s(**kw):
    return replace(get_settings(), **kw)


@pytest.fixture
def clean_settings(monkeypatch):
    """Resolve settings from a scrubbed environment (no project .env)."""
    monkeypatch.setattr("episteme.config._find_project_dotenv", lambda: None)
    for k in (
        "EPISTEME_DB_MODE",
        "EPISTEME_DB_TARGET",
        "PGDATABASE",
        "PGDATABASE_SECONDARY",
        "EPISTEME_PRODUCTION_DATABASE",
    ):
        monkeypatch.delenv(k, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_restricted_refuses_production():
    s = _s(db_mode="restricted", pg_database="episteme", production_database="episteme")
    with pytest.raises(ProductionDatabaseGuardError) as ei:
        check_database_allowed(s)
    msg = str(ei.value)
    assert "Refusing to open a connection to production database" in msg
    assert "EPISTEME_DB_MODE=unrestricted" in msg
    assert "EPISTEME_DB_TARGET=secondary" in msg


def test_restricted_allows_non_production():
    check_database_allowed(_s(db_mode="restricted", pg_database="episteme_test"))


def test_unrestricted_allows_production():
    check_database_allowed(_s(db_mode="unrestricted", pg_database="episteme"))


def test_read_only_allows_production_but_sets_pool_option():
    s = _s(db_mode="read-only", pg_database="episteme")
    check_database_allowed(s)
    assert pool_kwargs(s) == {"options": "-c default_transaction_read_only=on"}


def test_pool_kwargs_empty_for_other_modes():
    assert pool_kwargs(_s(db_mode="restricted")) == {}
    assert pool_kwargs(_s(db_mode="unrestricted")) == {}


def test_custom_production_name():
    s = _s(db_mode="restricted", pg_database="prod_db", production_database="prod_db")
    with pytest.raises(ProductionDatabaseGuardError):
        check_database_allowed(s)
    check_database_allowed(
        _s(db_mode="restricted", pg_database="episteme", production_database="prod_db")
    )


def test_default_mode_is_restricted(clean_settings):
    s = get_settings()
    assert s.db_mode == "restricted"
    assert s.db_target == "primary"
    assert s.production_database == "episteme"


def test_invalid_mode_is_rejected(clean_settings, monkeypatch):
    monkeypatch.setenv("EPISTEME_DB_MODE", "bogus")
    get_settings.cache_clear()
    with pytest.raises(_cfg.ConfigError):
        get_settings()


def test_secondary_target_resolves_secondary_database(clean_settings, monkeypatch):
    monkeypatch.setenv("PGDATABASE", "episteme")
    monkeypatch.setenv("PGDATABASE_SECONDARY", "episteme_test")
    monkeypatch.setenv("EPISTEME_DB_TARGET", "secondary")
    get_settings.cache_clear()
    assert get_settings().pg_database == "episteme_test"


def test_secondary_target_without_secondary_is_an_error(clean_settings, monkeypatch):
    monkeypatch.setenv("EPISTEME_DB_TARGET", "secondary")
    get_settings.cache_clear()
    with pytest.raises(_cfg.ConfigError):
        get_settings()


def test_db_modes_constant():
    assert DB_MODES == ("read-only", "restricted", "unrestricted")


@pytest.mark.pg
def test_read_only_mode_blocks_writes(monkeypatch):
    """Through the real pool, against episteme_test only."""
    import psycopg

    import episteme.data.db.connection as conn_mod

    monkeypatch.setenv("PGDATABASE", "episteme_test")
    monkeypatch.setenv("EPISTEME_DB_MODE", "read-only")
    get_settings.cache_clear()
    monkeypatch.setattr(conn_mod, "_POOL", None)
    try:
        with conn_mod.connection() as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                cur.execute("CREATE TABLE episteme._ro_probe (x int)")
    finally:
        if conn_mod._POOL is not None:
            conn_mod._POOL.close()
        get_settings.cache_clear()
