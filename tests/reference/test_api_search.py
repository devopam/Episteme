"""search_text (migration 0015) on the test partitions, with the fts_index expression
indexes built by the tool's own SQL (pg: episteme_test only)."""

from __future__ import annotations

import os

import psycopg
import pytest

from episteme.data.db import fts_index
from episteme.reference import _api, api_settings

from ._pbrer_db import MIGRATIONS, add_release

pytestmark = pytest.mark.pg

_MIGRATION = MIGRATIONS / "0015_api_layer.sql"
_MIGRATION_0016 = MIGRATIONS / "0016_api_perf.sql"
_MIGRATION_0017 = MIGRATIONS / "0017_api_no_timeouts.sql"
# Partitions of the test schema that get the index (schema.sql: pmc; 0010: dailymed).
# bookshelf (0002) stays without one: search_text must skip it.
_INDEXED = ("article_body_dailymed", "article_body_pmc")
_SOURCES = ("dailymed", "pmc", "bookshelf")


def _drop_indexes(conn) -> None:
    with conn.cursor() as cur:
        for part in _INDEXED:
            cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS episteme.{fts_index.index_name(part)}")


@pytest.fixture(scope="module")
def search_schema(ref_schema):
    """Migrations 0015 to 0017 again (idempotent), then the expression indexes, CONCURRENTLY."""
    conn = psycopg.connect(os.environ["TEST_PG_DSN"], autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(_MIGRATION.read_text(encoding="utf-8"))
            cur.execute(_MIGRATION_0016.read_text(encoding="utf-8"))
            cur.execute(_MIGRATION_0017.read_text(encoding="utf-8"))
            for part in _INDEXED:
                cur.execute(fts_index.build_sql(part))
        yield
        _drop_indexes(conn)
    finally:
        conn.close()


def _clear_bodies(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM episteme.article_body WHERE source = ANY (%s)", (list(_SOURCES),))
    conn.commit()


@pytest.fixture
def db(search_schema, ref_db):
    api_settings.sync(ref_db)
    _clear_bodies(ref_db)
    yield ref_db
    ref_db.rollback()
    _clear_bodies(ref_db)


def _body(conn, rows) -> None:
    """rows: (article_id, source, title, abstract, body_text, text_override)."""
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO episteme.article_body "
            "(article_id, source, title, abstract, body_text, text_override) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            rows,
        )
    conn.commit()


def _search(conn, query, sources=None) -> list[tuple]:
    with conn.cursor() as cur:
        # websearch_to_tsquery raises a NOTICE for stop-word-only input: keep output clean.
        cur.execute("SET LOCAL client_min_messages = warning")
        cur.execute("SELECT * FROM episteme_api.search_text(%s, %s::text[])", (query, sources))
        rows = cur.fetchall()
    conn.rollback()
    return rows


def _articles(rows) -> list[tuple]:
    return [r for r in rows if r[0] == "article"]


def _notes(rows) -> list[str]:
    return [r[9] for r in rows if r[0] == "note"]


def test_search_uses_index(db):
    _body(
        db,
        [
            (f"dailymed:x{i}", "dailymed", f"Label {i}", None, f"word{i} filler text", None)
            for i in range(200)
        ]
        + [("dailymed:hit", "dailymed", "Label", None, "rare zymoquartz", None)],
    )
    with db.cursor() as cur:
        cur.execute("ANALYZE episteme.article_body_dailymed")
        cur.execute("SET LOCAL enable_seqscan = off")
        expr = fts_index.fts_expression("b.")
        cur.execute(
            "EXPLAIN SELECT b.article_id FROM episteme.article_body b "
            f"WHERE b.source = ANY (ARRAY['dailymed']) AND {expr} "
            "@@ websearch_to_tsquery('english'::regconfig, 'zymoquartz')"
        )
        plan = "\n".join(r[0] for r in cur.fetchall())
    db.rollback()
    assert "Bitmap Index Scan" in plan, plan
    assert "article_body_dailymed_fts_idx" in plan, plan


def test_label_full_text_hit(db):
    _body(
        db,
        [
            ("dailymed:a", "dailymed", "METFORMIN label", None, "Warning: zymoquartz.", None),
            ("dailymed:b", "dailymed", "Other label", None, "x", "override has quillwort"),
            # A pmc body is not indexed (title + abstract only).
            ("pmc:PMC1", "pmc", "An article", "An abstract", "body zymoquartz", None),
            ("pmc:PMC2", "pmc", "Zymoquartz in the title", None, None, None),
        ],
    )
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.articles (id, source, source_file, year) VALUES "
            "('dailymed:a', 'dailymed', 'dailymed/part-1.parquet', 2024)"
        )
    db.commit()
    add_release(db, "dailymed", "20250101", [], latest=True)
    rows = _search(db, "zymoquartz", ["dailymed", "pmc"])
    hits = _articles(rows)
    assert sorted(r[1] for r in hits) == ["dailymed:a", "pmc:PMC2"]
    a = next(r for r in hits if r[1] == "dailymed:a")
    assert a[2] == "dailymed" and a[6] == "search"
    assert a[7] == "20250101"
    assert a[10] == "dailymed/part-1.parquet"
    assert a[8] == _licence(db, "dailymed")
    assert a[5] is not None and a[5] > 0
    # text_override counts for the labels.
    assert [r[1] for r in _articles(_search(db, "quillwort"))] == ["dailymed:b"]
    # Through the Python call path (the internal function, no cap).
    internal = _api.call(db, "search_text", "zymoquartz", ["dailymed"], None)
    db.rollback()
    assert [r[1] for r in internal if r[0] == "article"] == ["dailymed:a"]


def _licence(conn, source):
    with conn.cursor() as cur:
        cur.execute("SELECT episteme_api_internal.licence_for(%s)", (source,))
        (v,) = cur.fetchone()
    conn.rollback()
    return v


def test_unbuilt_and_uncovered_sources(db):
    _body(db, [("bookshelf:NBK1", "bookshelf", "zymoquartz book", None, None, None)])
    rows = _search(db, "zymoquartz", ["bookshelf", "pubchem", "dailymed"])
    assert _articles(rows) == []
    notes = _notes(rows)
    assert any(n == "not covered by full-text search: pubchem" for n in notes), notes
    assert any(n.startswith("full-text index not built") and "bookshelf" in n for n in notes)
    assert [r[2] for r in rows if r[0] == "missing"] == ["bookshelf"]
    # No sources asked: every covered source; those without an index are named.
    rows = _search(db, "zymoquartz")
    missing = {r[2] for r in rows if r[0] == "missing"}
    assert missing == {"pubmed", "openalex", "bookshelf", "ema_epar"}


def test_rank_then_article_id(db):
    _body(
        db,
        [
            ("dailymed:c", "dailymed", "aspirin", None, "aspirin", None),
            ("dailymed:b", "dailymed", "aspirin", None, "aspirin", None),
            ("dailymed:a", "dailymed", "label", None, "aspirin once", None),
        ],
    )
    hits = _articles(_search(db, "aspirin", ["dailymed"]))
    assert [r[1] for r in hits] == ["dailymed:b", "dailymed:c", "dailymed:a"]


def test_public_search_caps(db):
    limit = api_settings_limit(db)
    _body(
        db,
        [
            (f"dailymed:{i:03d}", "dailymed", "label", None, "zymoquartz", None)
            for i in range(limit + 5)
        ],
    )
    rows = _search(db, "zymoquartz", ["dailymed"])
    assert len(_articles(rows)) == limit
    assert _notes(rows)[-1] == f"truncated: {limit} of more than {limit}"


def api_settings_limit(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT episteme_api_internal.limit_for('search')")
        (n,) = cur.fetchone()
    conn.rollback()
    return n


@pytest.mark.parametrize(
    "query",
    [
        '"unbalanced quote',
        "it's 'quoted' \"text\"",
        "a & b | c ! d : e * f",
        "&|!:*()<->",
        "zymoquartz:* & !",
        "x" * 10_000,
        ("zymoquartz " * 2_000),
        "",
        None,
        "the and of a",
        "-zymoquartz",
        "\x01\x02 \\ % _",
    ],
)
def test_search_hostile_inputs(db, query):
    _body(db, [("dailymed:h", "dailymed", "label", None, "zymoquartz", None)])
    rows = _search(db, query, ["dailymed"])
    limit = api_settings_limit(db)
    assert len(_articles(rows)) <= limit
    assert len(rows) <= limit + 3
    if query in ("", None, "the and of a", "&|!:*()<->", "-zymoquartz"):
        assert len(rows) == 1 and rows[0][0] == "note", rows


def test_search_hostile_sources(db):
    rows = _search(db, "zymoquartz", ["x" * 10_000] * 3 + [None, "", "'; drop"])
    assert _articles(rows) == []
    notes = _notes(rows)
    assert len(notes) == 1 and notes[0].startswith("not covered by full-text search: ")
    assert len(notes[0]) <= len("not covered by full-text search: ") + 500


def test_empty_source_list_is_a_note(db):
    rows = _search(db, "zymoquartz", [])
    assert rows == [
        ("note", None, None, None, None, None, None, None, None, "no sources requested", None)
    ]


def test_scan_cap_ranks_within_the_first_matches(db):
    """With search_scan lowered to 5, a word on 12 rows is ranked within 5 candidates:
    one note and at most p_limit rows."""
    _body(
        db,
        [(f"dailymed:s{i:02d}", "dailymed", "label", None, "zymoquartz", None) for i in range(12)],
    )
    with db.cursor() as cur:
        cur.execute(
            "UPDATE episteme_api_internal.result_limit SET default_limit = 5, max_limit = 5 "
            "WHERE family = 'search_scan'"
        )
    db.commit()
    try:
        internal = _api.call(db, "search_text", "zymoquartz", ["dailymed"], 3)
        db.rollback()
        assert len([r for r in internal if r[0] == "article"]) == 3
        assert [r[9] for r in internal if r[0] == "note"] == [
            "ranked within the first 5 matches of dailymed"
        ]
        public = _search(db, "zymoquartz", ["dailymed"])
        assert len(_articles(public)) == 5  # all candidates; below the search cap of 20
        assert "ranked within the first 5 matches of dailymed" in _notes(public)
        # Ordered by score, then article_id.
        ids = [r[1] for r in _articles(public)]
        assert ids == sorted(ids)
    finally:
        api_settings.sync(db)


def _set_rank_chars(conn, chars: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE episteme_api_internal.result_limit SET default_limit = %s "
            "WHERE family = 'search_rank_chars'",
            (chars,),
        )
    conn.commit()


def test_label_rank_uses_leading_text_match_uses_all(db):
    """0016: a label ranks on its leading characters only; the match still reads all."""
    early = "zymoquartz " + "filler " * 40
    late = "filler " * 40 + "zymoquartz"  # word after character 100
    _body(
        db,
        [
            ("dailymed:early", "dailymed", "label", None, early, None),
            ("dailymed:late", "dailymed", "label", None, late, None),
        ],
    )
    _set_rank_chars(db, 100)
    try:
        internal = _api.call(db, "search_text", "zymoquartz", ["dailymed"], None)
        db.rollback()
        hits = [r for r in internal if r[0] == "article"]
        assert [r[1] for r in hits] == ["dailymed:early", "dailymed:late"]
        assert hits[0][5] > 0
        assert hits[1][5] == 0
    finally:
        api_settings.sync(db)


def test_rank_uses_first_chars_for_labels(db):
    """The label sources rank on the first search_rank_chars characters of body_text and
    text_override. A word after that point still matches (the index sees all), score 0.
    Other sources pass the raw columns (fts_doc ignores them): see the non-label test."""
    early = "zymoquartz " + "filler " * 40
    late = "filler " * 40 + "zymoquartz"
    _body(
        db,
        [
            ("dailymed:early", "dailymed", "label", None, early, None),
            ("dailymed:late", "dailymed", "label", None, late, None),
            ("dailymed:ovearly", "dailymed", "label", None, "x", early),
            ("dailymed:ovlate", "dailymed", "label", None, "x", late),
        ],
    )
    _set_rank_chars(db, 50)
    try:
        internal = _api.call(db, "search_text", "zymoquartz", ["dailymed"], None)
        db.rollback()
        score = {r[1]: r[5] for r in internal if r[0] == "article"}
        assert set(score) == {
            "dailymed:early",
            "dailymed:late",
            "dailymed:ovearly",
            "dailymed:ovlate",
        }
        assert score["dailymed:early"] > 0 and score["dailymed:ovearly"] > 0
        assert score["dailymed:late"] == 0 and score["dailymed:ovlate"] == 0
    finally:
        api_settings.sync(db)


def test_work_mem_restored(db):
    _body(db, [("dailymed:w", "dailymed", "label", None, "zymoquartz", None)])
    with db.cursor() as cur:
        cur.execute("SET LOCAL work_mem = '5MB'")
        cur.execute("SHOW work_mem")
        before = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM episteme_api_internal.search_text(%s, %s::text[], NULL)",
            ("zymoquartz", ["dailymed"]),
        )
        assert cur.fetchone()[0] == 1
        cur.execute("SHOW work_mem")
        after = cur.fetchone()[0]
    db.rollback()
    assert before == after == "5MB"


def test_budget_spent_names_unsearched_sources(db):
    """A budget of 1 ms, spent before the first source (the sleep runs first in the same
    statement; the lateral call reads its output, a boolean never NULL): one partial
    note naming every ready source, no article rows, work_mem as before."""
    _body(
        db,
        [
            ("dailymed:b", "dailymed", "label", None, "zymoquartz", None),
            ("pmc:PMC5", "pmc", "Zymoquartz in the title", None, None, None),
        ],
    )
    with db.cursor() as cur:
        cur.execute(
            "UPDATE episteme_api_internal.result_limit SET default_limit = 1, "
            "max_limit = greatest(max_limit, 1) WHERE family = 'time_budget_ms'"
        )
    db.commit()
    try:
        with db.cursor() as cur:
            cur.execute("SET LOCAL work_mem = '5MB'")
            cur.execute(
                "SELECT r.* FROM (SELECT pg_sleep(0.05) IS NULL) AS s(z) "
                "CROSS JOIN LATERAL episteme_api_internal.search_text("
                "CASE WHEN s.z IS NOT NULL THEN 'zymoquartz' END, "
                "ARRAY['dailymed', 'pmc'], NULL) AS r"
            )
            rows = cur.fetchall()
            cur.execute("SHOW work_mem")
            assert cur.fetchone()[0] == "5MB"
        db.rollback()
        assert _articles(rows) == []
        # Sources keep the order of the covered list: pmc before dailymed.
        assert _notes(rows) == ["partial: time budget of 1 ms spent; not searched: pmc, dailymed"]
    finally:
        api_settings.sync(db)


def test_budget_not_spent_gives_no_partial_note(db):
    _body(db, [("dailymed:b", "dailymed", "label", None, "zymoquartz", None)])
    rows = _api.call(db, "search_text", "zymoquartz", ["dailymed"], None)
    db.rollback()
    assert not [r for r in rows if r[0] == "note" and r[9].startswith("partial:")]


def test_non_label_rank_unchanged(db):
    """A non-label source ranks on the full fts_doc, as in 0015."""
    late = "filler " * 40 + "zymoquartz"
    _body(db, [("pmc:PMC9", "pmc", "An article", late, "body text", None)])
    _set_rank_chars(db, 100)
    try:
        internal = _api.call(db, "search_text", "zymoquartz", ["pmc"], None)
        db.rollback()
        with db.cursor() as cur:
            cur.execute(
                "SELECT ts_rank_cd(episteme_api_internal.fts_doc(b.source, b.title, b.abstract, "
                "b.body_text, b.text_override), "
                "websearch_to_tsquery('english', 'zymoquartz'))::real "
                "FROM episteme.article_body b WHERE b.article_id = 'pmc:PMC9'"
            )
            expected = cur.fetchone()[0]
        db.rollback()
        hits = [r for r in internal if r[0] == "article"]
        assert [r[1] for r in hits] == ["pmc:PMC9"]
        assert hits[0][5] == expected and expected > 0
    finally:
        api_settings.sync(db)
