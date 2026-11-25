"""search_terms (migration 0018) on fixture mesh_term rows and the test partitions, with
the fts_index expression indexes built by the tool's own SQL (pg: episteme_test only).
The descriptor UIs D91xxxxx are fixture values."""

from __future__ import annotations

import os
import threading

import psycopg
import pytest

from episteme.data.db import fts_index
from episteme.reference import api_settings

from ._pbrer_db import add_release

pytestmark = pytest.mark.pg

# Partitions that get the index (schema.sql: pmc; 0010: dailymed). bookshelf (0002) stays
# without one: search_terms must skip it.
_INDEXED = ("article_body_dailymed", "article_body_pmc")
_SOURCES = ("dailymed", "pmc", "bookshelf")
_RELEASE = "2026"
_SRC = "mesh/desc2026.gz"

_MI = "D9100001"
_ANA = "D9100002"
_STOP = "D9100003"
_ZQ = "D9100004"

# (descriptor_ui, term, term_kind, is_preferred, concept_relation)
_TERMS = [
    (_MI, "Myocardial Infarction", "name", True, None),
    (_MI, "Myocardial Infarction", "entry", True, None),
    (_MI, "Heart Attack", "entry", False, "NRW"),
    (_ANA, "Anaphylaxis", "name", True, None),
    (_ANA, "Anaphylactic Shock", "entry", False, "NRW"),
    # Every term is an English stop word: no query.
    (_STOP, "The", "name", True, None),
    (_STOP, "AS", "entry", True, None),
    (_ZQ, "Zymoquartz Syndrome", "name", True, None),
]

_COLS = "kind, article_id, source, title, year, score, relation, release_id, licence, note"


@pytest.fixture(scope="module")
def fts(ref_schema):
    """The expression indexes, CONCURRENTLY (ref_schema has applied 0018)."""
    conn = psycopg.connect(os.environ["TEST_PG_DSN"], autocommit=True)
    try:
        with conn.cursor() as cur:
            for part in _INDEXED:
                cur.execute(fts_index.build_sql(part))
        yield
        with conn.cursor() as cur:
            for part in _INDEXED:
                cur.execute(
                    f"DROP INDEX CONCURRENTLY IF EXISTS episteme.{fts_index.index_name(part)}"
                )
    finally:
        conn.close()


def _clear(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM episteme.article_body WHERE source = ANY (%s)", (list(_SOURCES),))
        cur.execute("DELETE FROM episteme.mesh_term")
    conn.commit()


def _seed_terms(conn) -> None:
    with conn.cursor() as cur:
        for ui, term, kind, pref, rel in _TERMS:
            cur.execute(
                "INSERT INTO episteme.mesh_term (descriptor_ui, term, term_norm, term_kind, "
                "is_preferred, concept_ui, concept_relation, mesh_release, source_file) "
                "VALUES (%s, %s, episteme_api_internal.term_norm(%s), %s, %s, %s, %s, %s, %s)",
                (ui, term, term, kind, pref, "M" + ui[1:], rel, _RELEASE, _SRC),
            )
    conn.commit()


@pytest.fixture
def db(fts, ref_db):
    api_settings.sync(ref_db)
    _clear(ref_db)
    _seed_terms(ref_db)
    yield ref_db
    ref_db.rollback()
    _clear(ref_db)


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


def _call(conn, uis, sources=None, *, public=False, limit=None) -> list[tuple]:
    with conn.cursor() as cur:
        if public:
            cur.execute(
                f"SELECT {_COLS} FROM episteme_api.search_terms(%s::text[], %s::text[])",
                (uis, sources),
            )
        else:
            cur.execute(
                f"SELECT {_COLS} FROM episteme_api_internal.search_terms("
                "%s::text[], %s::text[], %s)",
                (uis, sources, limit),
            )
        rows = cur.fetchall()
    conn.rollback()
    return rows


def _articles(rows) -> list[tuple]:
    return [r for r in rows if r[0] == "article"]


def _notes(rows) -> list[str]:
    return [r[9] for r in rows if r[0] == "note"]


def _tsq(conn, *terms: str) -> str:
    """The OR of websearch_to_tsquery('english', term) over terms, as text."""
    with conn.cursor() as cur:
        expr = " || ".join(["websearch_to_tsquery('english'::regconfig, %s)"] * len(terms))
        cur.execute(f"SELECT ({expr})::text", terms)
        (v,) = cur.fetchone()
    conn.rollback()
    return v


def _set_limit(conn, family: str, value: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE episteme_api_internal.result_limit SET default_limit = %s, "
            "max_limit = greatest(max_limit, %s) WHERE family = %s",
            (value, value, family),
        )
    conn.commit()


def test_entry_term_only_hit_with_exact_query_note(db):
    _body(db, [("dailymed:a", "dailymed", "Label", "Risk of heart attack.", None, None)])
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.articles (id, source, source_file, year) VALUES "
            "('dailymed:a', 'dailymed', 'dailymed/part-1.parquet', 2024)"
        )
    db.commit()
    add_release(db, "dailymed", "20250101", [], latest=True)
    with db.cursor() as cur:
        cur.execute(
            "SELECT article_id, relation, release_id, source_file, score "
            "FROM episteme_api_internal.search_terms(ARRAY[%s], ARRAY['dailymed'], NULL) "
            "WHERE kind = 'article'",
            (_MI,),
        )
        hits = cur.fetchall()
    db.rollback()
    assert len(hits) == 1
    assert hits[0][:4] == ("dailymed:a", _MI, "20250101", "dailymed/part-1.parquet")
    assert hits[0][4] > 0
    rows = _call(db, [_MI], ["dailymed"])
    # Terms in term_norm order, the duplicate "Myocardial Infarction" once.
    query = _tsq(db, "Heart Attack", "Myocardial Infarction")
    assert _notes(rows) == [f"terms used: {_MI} Myocardial Infarction; query: {query}"]
    assert rows[-1][0] == "article" and rows[-1][9] is None


def test_scan_cap_takes_lowest_article_ids_and_repeats(db):
    _body(
        db,
        [
            (f"pmc:PMC{i}", "pmc", f"Zymoquartz syndrome case {i}", None, None, None)
            for i in (3, 1, 2)
        ],
    )
    _set_limit(db, "search_scan", 2)
    try:
        first = _call(db, [_ZQ], ["pmc"])
        second = _call(db, [_ZQ], ["pmc"])
    finally:
        api_settings.sync(db)
    assert sorted(r[1] for r in _articles(first)) == ["pmc:PMC1", "pmc:PMC2"]
    assert f"capped: first 2 matches by article_id of pmc for {_ZQ}" in _notes(first)
    assert first == second


def test_budget_spent_gives_no_article_rows(db):
    _body(
        db,
        [
            ("dailymed:b", "dailymed", "label", None, "heart attack", None),
            ("pmc:PMC5", "pmc", "Heart attack in the title", None, None, None),
        ],
    )
    _set_limit(db, "time_budget_ms", 1)
    try:
        with db.cursor() as cur:
            cur.execute("SET LOCAL work_mem = '5MB'")
            cur.execute(
                f"SELECT {', '.join('r.' + c.strip() for c in _COLS.split(','))} "
                "FROM (SELECT pg_sleep(0.05) IS NULL) AS s(z) "
                "CROSS JOIN LATERAL episteme_api_internal.search_terms("
                "CASE WHEN s.z IS NOT NULL THEN ARRAY[%s, %s] END, "
                "ARRAY['dailymed', 'pmc'], NULL) AS r",
                (_MI, _ANA),
            )
            rows = cur.fetchall()
            cur.execute("SHOW work_mem")
            assert cur.fetchone()[0] == "5MB"
        db.rollback()
    finally:
        api_settings.sync(db)
    assert _articles(rows) == []
    assert {r[0] for r in rows} == {"note"}
    notes = _notes(rows)
    assert notes[:2] == [
        f"terms used: {_MI} Myocardial Infarction; query: "
        + _tsq(db, "Heart Attack", "Myocardial Infarction"),
        f"terms used: {_ANA} Anaphylaxis; query: " + _tsq(db, "Anaphylactic Shock", "Anaphylaxis"),
    ]
    # Sources keep the order of the covered list: pmc before dailymed.
    assert notes[2:] == [
        f"not run: time budget of 1 ms spent before {_MI} / pmc finished; search with fewer terms"
    ]


def _run_with_pmc_locked(conn, uis, sources) -> list[tuple]:
    """search_terms while a second connection holds an ACCESS EXCLUSIVE lock on the pmc
    partition for 2 s. Nothing before the candidate loop locks that partition, so the
    first pmc step starts before the deadline and finishes after it."""
    locker = psycopg.connect(os.environ["TEST_PG_DSN"], autocommit=False)
    timer = threading.Timer(2.0, locker.rollback)
    try:
        locker.execute("LOCK TABLE episteme.article_body_pmc IN ACCESS EXCLUSIVE MODE")
        timer.start()
        rows = _call(conn, uis, sources)
    finally:
        timer.cancel()
        timer.join()
        locker.close()
    return rows


def test_finished_capped_step_dropped_when_budget_spent_later(db):
    """The pmc step finishes (capped, with a match) after the deadline; the dailymed step
    is not run. Never partial: no article row and no capped note, only the terms used
    note and the not run note naming the step not run."""
    _body(
        db,
        [(f"pmc:PMC{i}", "pmc", "Zymoquartz syndrome", None, None, None) for i in range(2)]
        + [("dailymed:z", "dailymed", "Label", "zymoquartz syndrome", None, None)],
    )
    _set_limit(db, "search_scan", 1)
    _set_limit(db, "time_budget_ms", 500)
    try:
        rows = _run_with_pmc_locked(db, [_ZQ], ["dailymed", "pmc"])
    finally:
        api_settings.sync(db)
    assert _articles(rows) == []
    assert _notes(rows) == [
        f"terms used: {_ZQ} Zymoquartz Syndrome; query: " + _tsq(db, "Zymoquartz Syndrome"),
        f"not run: time budget of 500 ms spent before {_ZQ} / dailymed finished; "
        "search with fewer terms",
    ]


def test_budget_spent_at_a_descriptor_boundary(db):
    """The first descriptor finishes after the deadline: the second one is not run."""
    _body(
        db,
        [
            ("pmc:PMC1", "pmc", "Zymoquartz syndrome", None, None, None),
            ("pmc:PMC2", "pmc", "Heart attack", None, None, None),
        ],
    )
    _set_limit(db, "time_budget_ms", 500)
    try:
        rows = _run_with_pmc_locked(db, [_ZQ, _MI], ["pmc"])
    finally:
        api_settings.sync(db)
    assert _articles(rows) == []
    assert _notes(rows)[-1] == (
        f"not run: time budget of 500 ms spent before {_MI} / pmc finished; search with fewer terms"
    )


def test_unknown_and_duplicate_descriptors(db):
    _body(db, [("dailymed:a", "dailymed", "Label", "heart attack", None, None)])
    rows = _call(db, ["D0000000", _MI, _MI], ["dailymed"])
    notes = _notes(rows)
    assert notes[0] == "unknown descriptor: D0000000"
    assert len([n for n in notes if n.startswith(f"terms used: {_MI} ")]) == 1
    assert [r[1] for r in _articles(rows)] == ["dailymed:a"]


def test_stop_word_descriptor_has_no_query(db):
    _body(db, [("dailymed:a", "dailymed", "Label", "the as", None, None)])
    rows = _call(db, [_STOP], ["dailymed"])
    assert _articles(rows) == []
    assert _notes(rows) == [f"no searchable words in the terms of {_STOP}"]


def test_overlap_gives_one_row_per_article(db):
    _body(
        db,
        [
            ("dailymed:o", "dailymed", "Label", "heart attack and anaphylaxis", None, None),
            ("dailymed:h", "dailymed", "Label", "heart attack", None, None),
            ("pmc:PMC7", "pmc", "Anaphylactic shock and myocardial infarction", None, None, None),
        ],
    )
    rows = _call(db, [_ANA, _MI], ["dailymed", "pmc"])
    hits = {r[1]: r for r in _articles(rows)}
    assert sorted(hits) == ["dailymed:h", "dailymed:o", "pmc:PMC7"]
    assert hits["dailymed:o"][6] == _ANA and hits["dailymed:o"][9] == f"also found by: {_MI}"
    assert hits["pmc:PMC7"][6] == _ANA and hits["pmc:PMC7"][9] == f"also found by: {_MI}"
    assert hits["dailymed:h"][6] == _MI and hits["dailymed:h"][9] is None
    # Ordered by score descending, then article_id (COLLATE "C").
    arts = _articles(rows)
    keys = [(-r[5], r[1]) for r in arts]
    assert keys == sorted(keys)


def test_p_limit_cuts_rows(db):
    _body(
        db, [(f"dailymed:l{i}", "dailymed", "Label", "heart attack", None, None) for i in range(4)]
    )
    rows = _call(db, [_MI], ["dailymed"], limit=2)
    assert [r[1] for r in _articles(rows)] == ["dailymed:l0", "dailymed:l1"]


def test_public_cap_note(db):
    with db.cursor() as cur:
        cur.execute("SELECT episteme_api_internal.limit_for('search')")
        (limit,) = cur.fetchone()
    db.rollback()
    _body(
        db,
        [
            (f"dailymed:{i:03d}", "dailymed", "label", "heart attack", None, None)
            for i in range(limit + 5)
        ],
    )
    rows = _call(db, [_MI], ["dailymed"], public=True)
    assert len(_articles(rows)) == limit
    assert _notes(rows)[-1] == f"truncated: {limit} of more than {limit}"


def test_unbuilt_and_uncovered_sources(db):
    _body(db, [("bookshelf:NBK1", "bookshelf", "heart attack book", None, None, None)])
    rows = _call(db, [_MI], ["bookshelf", "pubchem", "dailymed"])
    assert _articles(rows) == []
    notes = _notes(rows)
    assert notes[0] == "not covered by full-text search: pubchem"
    assert notes[1] == "full-text index not built, not searched: bookshelf"
    assert [r[2] for r in rows if r[0] == "missing"] == ["bookshelf"]
    assert _call(db, [_MI], []) == [
        ("note", None, None, None, None, None, None, None, None, "no sources requested")
    ]


def test_work_mem_restored(db):
    _body(db, [("dailymed:w", "dailymed", "label", "heart attack", None, None)])
    with db.cursor() as cur:
        cur.execute("SET LOCAL work_mem = '5MB'")
        cur.execute(
            "SELECT count(*) FROM episteme_api_internal.search_terms(ARRAY[%s], "
            "ARRAY['dailymed'], NULL) WHERE kind = 'article'",
            (_MI,),
        )
        assert cur.fetchone()[0] == 1
        cur.execute("SHOW work_mem")
        assert cur.fetchone()[0] == "5MB"
    db.rollback()


_NONE = [("note", None, None, None, None, None, None, None, None, "no descriptors given")]
_TOO_MANY = [
    ("note", None, None, None, None, None, None, None, None, "at most 20 descriptors per search")
]


@pytest.mark.parametrize("uis", [None, [], [None], [""], [None, ""]])
def test_no_descriptors_is_one_note(db, uis):
    assert _call(db, uis) == _NONE
    assert _call(db, uis, public=True) == _NONE


def test_more_than_20_descriptors_is_refused(db):
    """Counted on the raw array: 21 entries are refused even when they repeat."""
    assert _call(db, [f"D{i:07d}" for i in range(21)]) == _TOO_MANY
    assert _call(db, [_MI] * 21) == _TOO_MANY
    assert _call(db, [f"D{i:07d}" for i in range(10_000)], public=True) == _TOO_MANY
    # 20 is allowed.
    rows = _call(db, [_MI] + [f"D{i:07d}" for i in range(19)], ["dailymed"])
    assert len([n for n in _notes(rows) if n.startswith("unknown descriptor: ")]) == 19


def test_hostile_descriptor_text(db):
    long = "x" * 10_000
    rows = _call(db, [long, "'; drop table x; --", "&|!:*()"], ["dailymed"])
    assert _articles(rows) == []
    notes = _notes(rows)
    assert len(notes) == 3
    assert all(n.startswith("unknown descriptor: ") for n in notes)
    assert all(len(n) <= len("unknown descriptor: ") + 500 for n in notes)


def test_candidate_query_uses_the_index(db):
    """The candidate query shape (ORDER BY article_id COLLATE "C" LIMIT) reads the
    partition through its full-text index, not through the primary key in order."""
    _body(
        db,
        [
            (f"dailymed:x{i}", "dailymed", f"Label {i}", None, f"word{i} filler text", None)
            for i in range(200)
        ]
        + [("dailymed:hit", "dailymed", "Label", None, "heart attack", None)],
    )
    with db.cursor() as cur:
        cur.execute("ANALYZE episteme.article_body_dailymed")
        cur.execute("SET LOCAL enable_seqscan = off")
        expr = fts_index.fts_expression("b.")
        cur.execute(
            "EXPLAIN SELECT b.article_id FROM episteme.article_body b "
            f"WHERE b.source = 'dailymed' AND {expr} "
            "@@ websearch_to_tsquery('english'::regconfig, 'heart attack') "
            'ORDER BY b.article_id COLLATE "C" LIMIT 2'
        )
        plan = "\n".join(r[0] for r in cur.fetchall())
    db.rollback()
    assert "Bitmap Index Scan" in plan, plan
    assert "article_body_dailymed_fts_idx" in plan, plan
    assert "pkey" not in plan, plan
