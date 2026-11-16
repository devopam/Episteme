"""Migration 0014: article_body.text becomes a VIRTUAL generated column (pg:
episteme_test only).

schema.sql has the final shape, so the pre-0014 table is built in a scratch schema
``t0014`` (old shape: a stored ``text`` column, LIST partitions by source). The
migration text is run with ``episteme.article_body`` replaced by
``t0014.article_body``: 0014 names the table only that way. The scratch schema is
dropped at the end; the ``episteme`` schema is not touched."""

import pytest

from episteme.data.article_schema import TEXT_JOIN_SQL, sql_join

from ._graph_schema import MIG

pytestmark = pytest.mark.pg

MIGRATION_0014 = MIG / "0014_article_text_virtual.sql"
SCRATCH = "t0014"

_OLD_SHAPE = f"""
DROP SCHEMA IF EXISTS {SCRATCH} CASCADE;
CREATE SCHEMA {SCRATCH};
CREATE TABLE {SCRATCH}.article_body (
    article_id  text NOT NULL,
    source      text NOT NULL,
    year        int,
    title       text,
    abstract    text,
    body_text   text,
    text        text,
    PRIMARY KEY (article_id, source)
) PARTITION BY LIST (source);
CREATE TABLE {SCRATCH}.article_body_pmc
    PARTITION OF {SCRATCH}.article_body FOR VALUES IN ('pmc');
CREATE TABLE {SCRATCH}.article_body_default
    PARTITION OF {SCRATCH}.article_body DEFAULT;
"""

# (article_id, source, title, abstract, body_text, stored text before 0014)
_ROWS = [
    # literature row: stored text equals the plain join
    ("lit", "pubmed", "T", "A", None, "T\n\nA"),
    # pmc-style row: stored text was whitespace-normalised, differs from the join
    ("pmc", "pmc", "T", "A  B", "C", "T\n\nA B\n\nC"),
    # structured row: the serializer wrote its own text, no parts
    ("structured", "chembl", None, None, None, "S"),
    # all-empty row
    ("empty", "chembl", None, None, None, None),
    # NULL text while a part is non-blank: must stay NULL (the '' sentinel)
    ("nulltext", "pubmed", "T", None, "B", None),
]


def _migration_sql() -> str:
    sql = MIGRATION_0014.read_text(encoding="utf-8")
    assert "episteme.article_body" in sql
    return sql.replace("episteme.article_body", f"{SCRATCH}.article_body")


@pytest.fixture
def scratch(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(_OLD_SHAPE)
        cur.executemany(
            f"INSERT INTO {SCRATCH}.article_body "
            "(article_id, source, title, abstract, body_text, text) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            _ROWS,
        )
    pg_conn.commit()
    try:
        yield pg_conn
    finally:
        pg_conn.rollback()
        with pg_conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCRATCH} CASCADE")
        pg_conn.commit()


def _texts(conn) -> dict[str, str | None]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT article_id, text FROM {SCRATCH}.article_body")
        return dict(cur.fetchall())


def _overrides(conn) -> dict[str, str | None]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT article_id, text_override FROM {SCRATCH}.article_body")
        return dict(cur.fetchall())


def _migrate(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(_migration_sql())
    conn.commit()


def test_0014_text_unchanged_for_every_shape(scratch):
    before = _texts(scratch)
    _migrate(scratch)
    assert _texts(scratch) == before
    with scratch.cursor() as cur:
        cur.execute(
            "SELECT a.attgenerated FROM pg_attribute a "
            "WHERE a.attrelid = %s::regclass AND a.attname = 'text'",
            (f"{SCRATCH}.article_body",),
        )
        assert cur.fetchone()[0] == "v"
        # the virtual column reaches every partition
        cur.execute(
            f"SELECT tableoid::regclass::text, text FROM {SCRATCH}.article_body "
            "WHERE article_id = 'pmc'"
        )
        assert cur.fetchone() == (f"{SCRATCH}.article_body_pmc", "T\n\nA B\n\nC")


def test_0014_null_text_with_parts_stays_null(scratch):
    _migrate(scratch)
    assert _texts(scratch)["nulltext"] is None
    overrides = _overrides(scratch)
    assert overrides["nulltext"] == ""
    # only that row gets the sentinel; the all-empty row keeps a NULL override
    assert overrides["empty"] is None
    assert [k for k, v in overrides.items() if v == ""] == ["nulltext"]


def test_0014_is_idempotent(scratch):
    before = _texts(scratch)
    _migrate(scratch)
    # simulate the later null-out: the literature row now reads the join
    with scratch.cursor() as cur:
        cur.execute(
            f"UPDATE {SCRATCH}.article_body SET text_override = NULL WHERE article_id = 'lit'"
        )
    scratch.commit()
    assert _texts(scratch) == before
    overrides = _overrides(scratch)

    _migrate(scratch)  # second run: guarded, no error, no change
    assert _texts(scratch) == before
    assert _overrides(scratch) == overrides
    assert overrides["lit"] is None  # the sentinel UPDATE did not run again


_EDGE_CASES = [
    (None, None, None),
    ("", "", ""),
    (" \t\r\n", None, "B"),
    (" ", None, "B"),  # NBSP is not blank
    (" ", "A", None),  # em space is not blank
    ("\x0b", None, None),  # vertical tab is not blank
    ("T", "A", "B"),
    (None, "A", None),
    ("T", None, "B"),
    (None, " ", "B"),
    (" a  b ", "\tx", None),  # no normalisation, tab kept
    ("T\n", "A\r\n", "B\n\n"),
]


def test_sql_join_matches_python_on_edge_cases(pg_conn):
    with pg_conn.cursor() as cur:
        for title, abstract, body_text in _EDGE_CASES:
            cur.execute(
                f"SELECT nullif({TEXT_JOIN_SQL}, '') "
                "FROM (VALUES (%s::text, %s::text, %s::text)) AS v(title, abstract, body_text)",
                (title, abstract, body_text),
            )
            got = cur.fetchone()[0]
            assert got == sql_join(title, abstract, body_text), (title, abstract, body_text)
    pg_conn.rollback()
