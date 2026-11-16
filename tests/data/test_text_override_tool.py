"""text_override tool: argument checks and SQL text (non-pg), null-out behaviour (pg)."""

from __future__ import annotations

from pathlib import Path

import pytest

import episteme.config as cfg
from episteme.data.article_schema import TEXT_JOIN_SQL
from episteme.data.db import text_override as tool

REPO_ROOT = Path(__file__).resolve().parents[2]


# ----------------------------------------------------------------------------- non-pg


class _NoDbConn:
    """A connection that must not be asked anything when the name is refused early."""

    def cursor(self):  # pragma: no cover - reached only on a bug
        raise AssertionError("the name check must run before any SQL")

    def rollback(self):  # pragma: no cover
        pass


@pytest.mark.parametrize(
    "name",
    ["article_body; drop", "articles_pubmed", "article_body_", "Article_body_pmc", "", "a b"],
)
def test_partition_name_refused_before_any_sql(name):
    with pytest.raises(tool.TextOverrideError):
        tool.validate_partition(_NoDbConn(), name)


def test_nullout_refuses_bad_partition_before_any_sql():
    with pytest.raises(tool.TextOverrideError):
        tool.nullout(_NoDbConn(), "article_body; drop", batch_rows=10, log=lambda m: None)


def test_batch_size_comes_from_settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EPISTEME_TEXT_NULLOUT_BATCH_ROWS", raising=False)
    cfg.get_settings.cache_clear()
    try:
        assert cfg.get_settings().text_nullout_batch_rows == 50_000
        monkeypatch.setenv("EPISTEME_TEXT_NULLOUT_BATCH_ROWS", "77")
        cfg.get_settings.cache_clear()
        assert cfg.get_settings().text_nullout_batch_rows == 77
    finally:
        cfg.get_settings.cache_clear()


def test_repack_without_yes_flag_exits_2(capsys):
    assert tool.main(["repack", "--partition", "article_body_pmc"]) == 2
    assert "--yes-repack" in capsys.readouterr().err


def test_repack_parser_accepts_flag():
    args = tool.build_parser().parse_args(["repack", "--partition", "x", "--yes-repack"])
    assert args.yes_repack is True


def test_precheck_needs_partition_or_all():
    with pytest.raises(SystemExit):
        tool.build_parser().parse_args(["precheck"])


def test_nullout_sql_compares_to_the_join_verbatim():
    # the null-out must compare against the very expression the virtual column uses
    update = tool.sql.SQL("x" + TEXT_JOIN_SQL)
    assert TEXT_JOIN_SQL in update.as_string()
    src = (REPO_ROOT / "src/episteme/data/db/text_override.py").read_text(encoding="utf-8")
    assert "text_override IS NOT NULL AND text_override <> ''" in src
    assert "TEXT_JOIN_SQL" in src


# --------------------------------------------------------------------------------- pg


@pytest.fixture
def body(pg_conn):
    """episteme_test with the final schema; rows land in article_body_pmc."""
    with (
        pg_conn.cursor() as cur,
        open(REPO_ROOT / "src/episteme/data/db/extensions.sql") as ext,
        open(REPO_ROOT / "src/episteme/data/db/schema.sql") as sch,
    ):
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute(ext.read())
        cur.execute(sch.read())
    pg_conn.commit()
    return pg_conn


def _insert(conn, rows):
    with conn.cursor() as cur:
        for aid, title, abstract, body_text, override in rows:
            cur.execute(
                "INSERT INTO episteme.article_body "
                "(article_id, source, title, abstract, body_text, text_override) "
                "VALUES (%s, 'pmc', %s, %s, %s, %s)",
                (aid, title, abstract, body_text, override),
            )
    conn.commit()


def _fixture_rows():
    return [
        ("a1", "T", "A", "B", "T\n\nA\n\nB"),  # equals the join
        ("a2", "T", None, "B", "T\n\nB"),  # equals the join
        ("a3", "T", "A", "B", "different text"),  # differs
        ("a4", "T", "A", "B", ""),  # NULL sentinel
        ("a5", "T", "A", "B", None),  # already NULL
        ("a6", "  ", None, "B", "B"),  # blank title: join is just B
    ]


def _overrides(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT article_id, text_override FROM episteme.article_body ORDER BY 1")
        out = dict(cur.fetchall())
    conn.commit()
    return out


def _texts(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT article_id, text FROM episteme.article_body ORDER BY 1")
        out = dict(cur.fetchall())
    conn.commit()
    return out


@pytest.mark.pg
def test_nullout_nulls_only_equal_rows(body):
    _insert(body, _fixture_rows())
    res = tool.nullout(body, "article_body_pmc", batch_rows=2, log=lambda m: None)
    ov = _overrides(body)
    assert res["rows"] == 6
    assert res["nulled"] == 3
    assert ov["a1"] is None and ov["a2"] is None and ov["a6"] is None


@pytest.mark.pg
def test_nullout_keeps_rows_that_differ(body):
    _insert(body, _fixture_rows())
    tool.nullout(body, "article_body_pmc", batch_rows=3, log=lambda m: None)
    assert _overrides(body)["a3"] == "different text"


@pytest.mark.pg
def test_nullout_keeps_the_null_sentinel(body):
    _insert(body, _fixture_rows())
    tool.nullout(body, "article_body_pmc", batch_rows=50, log=lambda m: None)
    assert _overrides(body)["a4"] == ""
    assert _texts(body)["a4"] is None


@pytest.mark.pg
def test_nullout_is_restartable(body):
    _insert(body, _fixture_rows())
    first = tool.nullout(body, "article_body_pmc", batch_rows=2, log=lambda m: None)
    second = tool.nullout(body, "article_body_pmc", batch_rows=2, log=lambda m: None)
    assert first["nulled"] == 3
    assert second["nulled"] == 0 and second["rows"] == 6


@pytest.mark.pg
def test_text_unchanged_after_nullout(body):
    _insert(body, _fixture_rows())
    before = _texts(body)
    tool.nullout(body, "article_body_pmc", batch_rows=2, log=lambda m: None)
    assert _texts(body) == before


@pytest.mark.pg
def test_precheck_clean_after_0014_shape(body):
    _insert(body, _fixture_rows())
    res = tool.precheck(body, "article_body_pmc", batch_rows=2, log=lambda m: None)
    assert res["column"] == "text_override"
    # a5 has NULL override with non-blank parts: it reads as the join, so it is counted
    assert res["null_with_parts"] == 1
    assert res["empty_string"] == 1
    assert res["rows"] == 6


@pytest.mark.pg
def test_nullout_refuses_a_non_partition_name(body):
    with pytest.raises(tool.TextOverrideError):
        tool.nullout(body, "article_body_nope", batch_rows=10, log=lambda m: None)
