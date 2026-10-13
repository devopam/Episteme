"""admin_dsn: the table-owner DSN for bulk-load index work. No DB."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from psycopg.conninfo import conninfo_to_dict

from episteme.config import ConfigError
from episteme.data.db import connection
from episteme.data.db.guard import ProductionDatabaseGuardError


def _settings(**over):
    base = {
        "db_mode": "unrestricted",
        "pg_database": "episteme_test",
        "production_database": "episteme",
        "pg_host": "localhost",
        "pg_port": 5433,
        "sys_admin_password": "p w'x",
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_admin_dsn_uses_sys_admin_role_and_quotes_password():
    d = conninfo_to_dict(connection.admin_dsn(_settings()))
    assert d["user"] == "episteme_sys_admin"
    assert d["password"] == "p w'x"
    assert d["dbname"] == "episteme_test"
    assert d["host"] == "localhost"
    assert d["port"] == "5433"


def test_admin_dsn_runs_the_guard_before_anything_else():
    # empty password too: the guard must fire first
    with pytest.raises(ProductionDatabaseGuardError):
        connection.admin_dsn(
            _settings(db_mode="restricted", pg_database="episteme", sys_admin_password="")
        )


def test_admin_dsn_requires_the_password():
    with pytest.raises(ConfigError, match="EPISTEME_SYS_ADMIN_PASSWORD"):
        connection.admin_dsn(_settings(sys_admin_password=""))


def test_admin_dsn_from_settings_is_blocked_in_non_pg_tests():
    with pytest.raises(RuntimeError, match="blocked"):
        connection.admin_dsn_from_settings()
