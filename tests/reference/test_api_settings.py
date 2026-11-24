"""API result-limit settings (no database)."""

from __future__ import annotations

from pathlib import Path

import pytest

import episteme.config as cfg
from episteme.data.db import api_grants
from episteme.reference import api_settings

_KEYS = [f"EPISTEME_API_LIMIT_{f.upper()}" for f in cfg.API_RESULT_LIMITS]


@pytest.fixture
def clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_api_limits_defaults(clean):
    assert cfg.api_result_limits() == {
        "regulatory": (200, 1000),
        "label": (200, 2000),
        "trials": (200, 1000),
        "faers": (500, 5000),
        "literature": (50, 500),
        "graph": (50, 500),
        "search": (20, 200),
        "code": (1, 1),
        "resolve": (10, 50),
        "graph_scan": (20000, 20000),
        "search_scan": (5000, 5000),
        "literature_window": (5, 5),
        "search_rank_chars": (2000, 2000),
        "literature_topic_scan": (3000000, 3000000),
        "literature_topic_chunk": (500, 500),
        "time_budget_ms": (20000, 20000),
        "search_work_mem_mb": (256, 256),
    }


def test_api_limit_env_override(clean):
    clean.setenv("EPISTEME_API_LIMIT_SEARCH", "5,50")
    out = cfg.api_result_limits()
    assert out["search"] == (5, 50)
    assert out["label"] == (200, 2000)


@pytest.mark.parametrize("bad", ["0,5", "10,5", "x", "5", "1,2,3", "-1,5"])
def test_api_limit_env_rejects_bad(clean, bad):
    clean.setenv("EPISTEME_API_LIMIT_SEARCH", bad)
    with pytest.raises(ValueError):
        cfg.api_result_limits()


def test_sync_is_one_transaction_with_counts(clean):
    calls = []

    class Cur:
        def execute(self, sql, params=None):
            calls.append(("x", sql.split()[0]))

        def executemany(self, sql, rows):
            calls.append(("m", len(list(rows))))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Tx:
        def __enter__(self):
            calls.append(("begin", 0))

        def __exit__(self, *a):
            calls.append(("end", 0))
            return False

    class Conn:
        def transaction(self):
            return Tx()

        def cursor(self):
            return Cur()

    counts = api_settings.sync(Conn())
    assert counts == {
        "source_subset": len(cfg.LOOKUP_SOURCE_SUBSETS),
        "result_limit": len(cfg.API_RESULT_LIMITS),
    }
    assert calls[0] == ("begin", 0) and calls[-1] == ("end", 0)
    assert [c for c in calls if c[0] == "x"] == [("x", "DELETE"), ("x", "DELETE")]


def test_sync_writes_search_scan(clean):
    written: list[tuple] = []

    class Cur:
        def execute(self, sql, params=None):
            pass

        def executemany(self, sql, rows):
            if "result_limit" in sql:
                written.extend(rows)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Conn:
        def transaction(self):
            return Cur()

        def cursor(self):
            return Cur()

    api_settings.sync(Conn())
    assert ("search_scan", 5000, 5000) in written


# ---------------------------------------------------------------------------
# The reader role's statement_timeout (ruling R17, second guard)
# ---------------------------------------------------------------------------
class _RoleConn:
    """A connection whose role check answers ``exists``; records the statements."""

    def __init__(self, exists: bool):
        self.exists = exists
        self.statements: list[str] = []

    def transaction(self):
        return self._Cm()

    def cursor(self):
        conn = self

        class Cur:
            def execute(self, query, params=None):
                text = query if isinstance(query, str) else query.as_string(None)
                conn.statements.append(text)

            def fetchone(self):
                return (conn.exists,)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return Cur()

    class _Cm:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False


def test_reader_timeout_is_set_on_the_role():
    conn = _RoleConn(exists=True)
    assert api_settings.apply_reader_settings(conn, 30) == "30s"
    alter = [s for s in conn.statements if s.startswith("ALTER ROLE")]
    assert alter == ["ALTER ROLE \"episteme_api_reader\" SET statement_timeout = '30s'"]


def test_reader_timeout_zero_means_no_timeout():
    conn = _RoleConn(exists=True)
    assert api_settings.apply_reader_settings(conn, 0) == "0"
    assert conn.statements[-1] == "ALTER ROLE \"episteme_api_reader\" SET statement_timeout = '0'"


def test_reader_timeout_skipped_without_the_role():
    conn = _RoleConn(exists=False)
    assert api_settings.apply_reader_settings(conn, 30) is None
    assert not [s for s in conn.statements if s.startswith("ALTER ROLE")]


def test_reader_timeout_rejects_a_negative_value():
    with pytest.raises(ValueError):
        api_settings.apply_reader_settings(_RoleConn(exists=True), -1)


def test_reader_timeout_config_default_and_override(clean):
    clean.delenv("EPISTEME_API_READER_STATEMENT_TIMEOUT_S", raising=False)
    cfg.get_settings.cache_clear()
    try:
        assert cfg.get_settings().api_reader_statement_timeout_s == 30
        clean.setenv("EPISTEME_API_READER_STATEMENT_TIMEOUT_S", "5")
        cfg.get_settings.cache_clear()
        assert cfg.get_settings().api_reader_statement_timeout_s == 5
    finally:
        cfg.get_settings.cache_clear()


def test_reader_password_is_not_in_the_settings_repr(clean):
    clean.setenv("EPISTEME_API_READER_PASSWORD", "s3cret-value")
    cfg.get_settings.cache_clear()
    try:
        settings = cfg.get_settings()
        assert settings.api_reader_password == "s3cret-value"
        assert "s3cret-value" not in repr(settings)
    finally:
        cfg.get_settings.cache_clear()


def test_env_example_names_the_reader_keys():
    text = (Path(__file__).resolve().parents[2] / ".env.example").read_text("utf-8")
    assert "# EPISTEME_API_READER_STATEMENT_TIMEOUT_S=30" in text
    assert "\nEPISTEME_API_READER_PASSWORD=" in text


def test_owner_grants_apply_the_tables_file_then_the_aact_file():
    conn = _RoleConn(exists=True)
    assert api_settings.apply_owner_grants(conn) is True
    assert conn.statements[1:] == [
        api_grants.OWNER_GRANTS.read_text(encoding="utf-8"),
        api_grants.AACT_GRANTS.read_text(encoding="utf-8"),
    ]
    assert api_grants.OWNER_GRANTS.name == "0015_api_tables.sql"
    assert api_grants.AACT_GRANTS.name == "0015_api_aact.sql"


def test_aact_grants_apply_only_the_aact_file():
    """The AACT switch's step (ruling R21): the role check and the AACT file only."""
    conn = _RoleConn(exists=True)
    assert api_grants.apply_aact_grants(conn) is True
    assert conn.statements[1:] == [api_grants.AACT_GRANTS.read_text(encoding="utf-8")]
    missing = _RoleConn(exists=False)
    assert api_grants.apply_aact_grants(missing) is False
    assert len(missing.statements) == 1


def test_owner_grants_skipped_without_the_role():
    conn = _RoleConn(exists=False)
    assert api_settings.apply_owner_grants(conn) is False
    assert len(conn.statements) == 1  # only the role check
