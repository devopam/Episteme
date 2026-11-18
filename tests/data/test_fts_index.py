"""fts_index tool: allowlist, free-space refusal and SQL text (non-pg); a real
CONCURRENTLY build on the test partition (pg: episteme_test only)."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

import episteme.config as cfg
from episteme.data.db import fts_index as tool

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_DIR = REPO_ROOT / "src/episteme/data/db"
MIGRATION = DB_DIR / "migrations/0015_api_layer.sql"
SCHEMA = DB_DIR / "schema.sql"
_MIGRATIONS = (
    "0002_container_and_book_parts.sql",
    "0006_pubmed_graph.sql",
    "0010_regulatory_sources.sql",
    "0011_label_section_ordinal.sql",
    "0014_article_text_virtual.sql",
    "0015_api_layer.sql",
)


# ----------------------------------------------------------------------------- non-pg


class _NoDbConn:
    """A connection that must not be asked anything when the name is refused early."""

    def cursor(self):  # pragma: no cover - reached only on a bug
        raise AssertionError("the allowlist check must run before any SQL")

    def rollback(self):  # pragma: no cover
        pass


def test_partition_allowlist():
    assert tool.FTS_PARTITIONS == (
        "article_body_pubmed",
        "article_body_pmc",
        "article_body_openalex",
        "article_body_bookshelf",
        "article_body_dailymed",
        "article_body_ema_epar",
    )
    # The preprints live in article_body_default: not covered.
    assert "article_body_default" not in tool.FTS_PARTITIONS
    for name in ("article_body_default", "article_body_pubchem", "articles_pubmed", "x; drop"):
        with pytest.raises(tool.FtsRefused):
            tool.check_allowed(name)
    for name in tool.FTS_PARTITIONS:
        assert tool.check_allowed(name) == name


@pytest.mark.parametrize("name", ["article_body_default", "article_body_apollo", "foo"])
def test_main_refuses_partition_outside_allowlist_exit_2(name, capsys):
    assert tool.main(["build", "--partition", name, "--yes"]) == 2
    assert "not in the full-text allowlist" in capsys.readouterr().err


def test_build_refuses_before_any_sql():
    with pytest.raises(tool.FtsRefused):
        tool.build(_NoDbConn(), "article_body_default", yes=True)


def test_free_space_refusal():
    # 10 GB partition needs 20 GB free; 19 GB is refused, 20 GB passes.
    gb = 1024**3
    with pytest.raises(tool.FtsSpaceError) as exc:
        tool.check_free_space(
            "article_body_pmc", rel_bytes=10 * gb, free_bytes=19 * gb, directory="E:/pgdata"
        )
    assert "article_body_pmc" in str(exc.value)
    tool.check_free_space(
        "article_body_pmc", rel_bytes=10 * gb, free_bytes=20 * gb, directory="E:/pgdata"
    )


def test_free_space_refusal_in_build(monkeypatch):
    """build() refuses (FtsSpaceError) with stubbed sizes and runs no CREATE INDEX."""
    executed: list[str] = []

    monkeypatch.setattr(tool, "validate_partition", lambda conn, p: p)
    monkeypatch.setattr(tool, "index_state", lambda conn, p: None)
    monkeypatch.setattr(tool, "relation_size", lambda conn, p: 100)
    monkeypatch.setattr(tool, "index_directory", lambda conn: "E:/pgdata")

    class _Conn:
        autocommit = False

        def cursor(self):  # pragma: no cover - reached only on a bug
            executed.append("cursor")
            raise AssertionError("no SQL after the space refusal")

    with pytest.raises(tool.FtsSpaceError):
        tool.build(_Conn(), "article_body_pmc", yes=True, free_bytes_of=lambda d: 199)
    assert executed == []


def test_free_space_unknown_directory_refused(monkeypatch):
    monkeypatch.setattr(tool, "validate_partition", lambda conn, p: p)
    monkeypatch.setattr(tool, "index_state", lambda conn, p: None)
    monkeypatch.setattr(tool, "relation_size", lambda conn, p: 100)
    monkeypatch.setattr(tool, "index_directory", lambda conn: None)
    with pytest.raises(tool.FtsSpaceError) as exc:
        tool.build(object(), "article_body_pmc", yes=True, free_bytes_of=lambda d: 10**15)
    assert "--data-dir" in str(exc.value)


def test_invalid_index_refused(monkeypatch):
    monkeypatch.setattr(tool, "validate_partition", lambda conn, p: p)
    monkeypatch.setattr(tool, "index_state", lambda conn, p: "invalid")
    with pytest.raises(tool.FtsRefused) as exc:
        tool.build(object(), "article_body_pmc", yes=True, free_bytes_of=lambda d: 10**15)
    assert "DROP INDEX CONCURRENTLY" in str(exc.value)
    assert "article_body_pmc_fts_idx" in str(exc.value)


def test_build_sql_uses_concurrently_and_identifier():
    from psycopg import sql

    stmt = tool.build_sql("article_body_dailymed")
    assert isinstance(stmt, sql.Composed)
    text = stmt.as_string(None)
    assert text == (
        'CREATE INDEX CONCURRENTLY IF NOT EXISTS "article_body_dailymed_fts_idx" '
        'ON "episteme"."article_body_dailymed" USING gin '
        "(episteme_api_internal.fts_doc(source, title, abstract, body_text, text_override))"
    )
    with pytest.raises(tool.FtsRefused):
        tool.build_sql("article_body_default")


def test_index_name():
    assert tool.index_name("article_body_ema_epar") == "article_body_ema_epar_fts_idx"


@pytest.mark.parametrize("path", [MIGRATION, SCHEMA])
def test_search_text_uses_the_index_expression(path):
    """The query expression in search_text is the index expression (with the b. alias),
    so the planner can use the index. EXPLAIN cannot see inside the plpgsql body."""
    text = path.read_text(encoding="utf-8")
    body = text[text.index("FUNCTION episteme_api_internal.search_text(") :]
    body = body[: body.index("\n$$;")]
    expr = tool.fts_expression("b.")
    assert expr == (
        "episteme_api_internal.fts_doc(b.source, b.title, b.abstract, b.body_text, b.text_override)"
    )
    assert body.count(expr) == 2  # the match and the rank


@pytest.mark.parametrize("path", [MIGRATION, SCHEMA])
def test_sql_source_lists_match_the_allowlist(path):
    text = path.read_text(encoding="utf-8")
    body = text[text.index("FUNCTION episteme_api_internal.search_text(") :]
    m = re.search(r"c_covered\s+constant\s+text\[\]\s*:=\s*ARRAY\[([^\]]*)\]", body)
    assert m, "search_text must declare c_covered"
    covered = tuple(s.strip().strip("'") for s in m.group(1).split(","))
    assert covered == tool.FTS_SOURCES
    doc = text[text.index("FUNCTION episteme_api_internal.fts_doc(") :]
    doc = doc[: doc.index("\n$$;")]
    m = re.search(r"p_source IN \(([^)]*)\)", doc)
    assert m
    full = tuple(s.strip().strip("'") for s in m.group(1).split(","))
    assert full == tool.FULL_TEXT_SOURCES == ("dailymed", "ema_epar")
    assert "'english'::pg_catalog.regconfig" in doc
    assert "IMMUTABLE" in doc


def test_lock_timeout_default_and_override(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EPISTEME_FTS_LOCK_TIMEOUT_S", raising=False)
    cfg.get_settings.cache_clear()
    try:
        assert cfg.get_settings().fts_lock_timeout_s == 60
        monkeypatch.setenv("EPISTEME_FTS_LOCK_TIMEOUT_S", "7")
        cfg.get_settings.cache_clear()
        assert cfg.get_settings().fts_lock_timeout_s == 7
    finally:
        cfg.get_settings.cache_clear()


def test_env_example_names_the_tunable():
    assert "# EPISTEME_FTS_LOCK_TIMEOUT_S=60" in (REPO_ROOT / ".env.example").read_text("utf-8")


# ----------------------------------------------------------------------------- pg


@pytest.fixture
def fts_conn(ref_schema_data):
    import psycopg

    conn = psycopg.connect(os.environ["TEST_PG_DSN"], autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DROP INDEX CONCURRENTLY IF EXISTS episteme.article_body_dailymed_fts_idx")
            cur.execute("DELETE FROM episteme.article_body WHERE source = 'dailymed'")
        conn.close()


@pytest.fixture(scope="module")
def ref_schema_data():
    """schema.sql and the migrations the partitions and functions need (as
    tests/reference/_pbrer_db.build_schema)."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN not set")
    if conninfo_to_dict(dsn).get("dbname") != "episteme_test":
        pytest.fail("TEST_PG_DSN does not target episteme_test; refusing to DROP SCHEMA")
    conn = psycopg.connect(dsn, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
            cur.execute((DB_DIR / "extensions.sql").read_text("utf-8"))
            cur.execute((DB_DIR / "schema.sql").read_text("utf-8"))
            # 0002: article_body_bookshelf; 0010: article_body_dailymed / _ema_epar.
            for name in _MIGRATIONS:
                cur.execute((DB_DIR / "migrations" / name).read_text("utf-8"))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.pg
def test_build_creates_a_valid_index_and_status_reports_it(fts_conn):
    with fts_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.article_body (article_id, source, title, body_text) "
            "VALUES ('dailymed:fts1', 'dailymed', 'A label', 'Contains zymoquartz.')"
        )
    fts_conn.commit()
    logs: list[str] = []
    out = tool.build(
        fts_conn,
        "article_body_dailymed",
        yes=True,
        free_bytes_of=lambda d: 10**15,
        directory="stub",
        lock_timeout_s=5,
        log=logs.append,
    )
    assert out["built"] is True
    assert tool.index_state(fts_conn, "article_body_dailymed") == "valid"
    # A second run finds the valid index and does nothing.
    again = tool.build(
        fts_conn,
        "article_body_dailymed",
        yes=True,
        free_bytes_of=lambda d: 10**15,
        directory="stub",
        log=logs.append,
    )
    assert again["built"] is False
    rows = {r["partition"]: r for r in tool.status(fts_conn)}
    assert rows["article_body_dailymed"]["index"] == "valid"
    assert rows["article_body_dailymed"]["index_bytes"] > 0
    # The test schema has no pubmed or openalex partition (migrations 0007, 0012).
    assert rows["article_body_openalex"]["exists"] is False
    with fts_conn.cursor() as cur:
        cur.execute("SHOW lock_timeout")
        assert cur.fetchone()[0] != "5s"  # reset after the build
    fts_conn.rollback()


@pytest.mark.pg
def test_build_without_yes_builds_nothing(fts_conn):
    out = tool.build(
        fts_conn,
        "article_body_dailymed",
        yes=False,
        free_bytes_of=lambda d: 10**15,
        directory="stub",
        log=lambda m: None,
    )
    assert out["built"] is False
    assert tool.index_state(fts_conn, "article_body_dailymed") is None


@pytest.mark.pg
def test_build_refuses_partition_that_does_not_exist(fts_conn):
    # article_body_openalex is allowed but migration 0012 is not applied here.
    with pytest.raises(tool.FtsRefused):
        tool.build(
            fts_conn,
            "article_body_openalex",
            yes=True,
            free_bytes_of=lambda d: 10**15,
            directory="stub",
            log=lambda m: None,
        )
