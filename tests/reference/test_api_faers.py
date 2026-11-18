"""faers_counts as a SQL function (migration 0015), against the former Python query
path in ``_legacy`` (pg: episteme_test only), and a source check of the FAERS SQL
(non-pg)."""

from __future__ import annotations

import os
import re
from datetime import date

import psycopg
import pytest

from episteme.config import api_result_limits
from episteme.reference import api_settings, lookups

from . import _legacy as legacy
from ._parity import assert_same_fields
from ._pbrer_db import KW, MIGRATIONS, REPO_ROOT

_MIGRATION = MIGRATIONS / "0015_api_layer.sql"
_SIGNALS = REPO_ROOT / "src/episteme/data/regulatory/faers/signals.py"
_FROM, _TO = date(2025, 2, 15), date(2025, 6, 30)  # overlaps 2025Q1 and 2025Q2


# ---------------------------------------------------------------------------
# non-pg
# ---------------------------------------------------------------------------
def _faers_function_sql() -> str:
    text = _MIGRATION.read_text(encoding="utf-8")
    start = text.find("CREATE OR REPLACE FUNCTION episteme_api_internal.faers_counts(")
    assert start >= 0, "migration 0015 has no episteme_api_internal.faers_counts"
    body_start = text.index("$$", start)
    body_end = text.index("$$;", body_start + 2)
    return text[start : body_end + 3]


def test_no_meddra_hierarchy_used():
    """No MedDRA hierarchy level (SOC, HLGT, HLT, LLT) is read: the terms are quoted as
    FAERS stores them (owner decision)."""
    level = re.compile(r"\b(?:soc|hlgt|hlt|llt)(?:_\w+)?\b", re.IGNORECASE)
    for name, text in (
        ("faers_counts", _faers_function_sql()),
        ("signals.py", _SIGNALS.read_text(encoding="utf-8")),
    ):
        assert not level.findall(text), f"{name} names a MedDRA hierarchy level"
    assert "episteme.faers_term" in _faers_function_sql()


def test_faers_texts_match_the_sql():
    """The label and notes that lookups exports are the ones the SQL function returns."""
    body = _faers_function_sql()
    for text in (
        lookups.FAERS_LABEL,
        lookups.FAERS_QUARTER_RULE,
        lookups.FAERS_OUTCOME_NOTE,
        lookups.FAERS_TERM_NOTE,
        lookups.FAERS_SPLIT_GAP,
    ):
        assert f"'{text}'" in body, text


# ---------------------------------------------------------------------------
# pg
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def api_schema(ref_schema):
    """Apply migration 0015 on top of the module schema (it is idempotent)."""
    conn = psycopg.connect(os.environ["TEST_PG_DSN"], autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute(_MIGRATION.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db(api_schema, ref_db, clean_substance):
    """The ref_db substances (1 metformin, 2 its salt, 3 aspirin) and the API settings
    from config."""
    api_settings.sync(ref_db)
    yield ref_db
    ref_db.rollback()


def _rows(conn, query, params=()):
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def _quarters(conn) -> None:
    """The quarter fixture of test_lookups: moiety 1 in 2024Q4..2025Q2, aspirin in 2025Q1;
    totals for 2025Q1 and 2025Q2 only."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.faers_moiety_quarter "
            "(substance_key, quarter, reports_suspect, reports_any) VALUES "
            "(1, '2024-10-01', 3, 4), (1, '2025-01-01', 5, 8), (1, '2025-04-01', 6, 9), "
            "(3, '2025-01-01', 50, 60)"
        )
        cur.execute(
            "INSERT INTO episteme.faers_quarter_totals (quarter, reports) VALUES "
            "('2025-01-01', 1000), ('2025-04-01', 1100)"
        )
    conn.commit()


def _splits_and_terms(conn) -> dict[str, int]:
    """Outcome rows and event rows of moiety 1. Returns the term ids by term."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.faers_moiety_quarter_outcome "
            "(substance_key, quarter, serious, outcome, reports) VALUES "
            "(1, '2025-01-01', false, 'none', 2), (1, '2025-01-01', true, 'HO', 3), "
            "(1, '2025-01-01', true, 'DE', 1), (1, '2025-04-01', true, 'OT', 6), "
            "(1, '2024-10-01', true, 'HO', 3), (3, '2025-01-01', true, 'DE', 50)"
        )
        terms = {}
        for term in ("Nausea", "NAUSEA", "Lactic acidosis"):
            cur.execute(
                "INSERT INTO episteme.faers_term (term, term_norm) VALUES (%s, lower(%s)) "
                "RETURNING term_id",
                (term, term),
            )
            terms[term] = cur.fetchone()[0]
        # The event table holds the lowest id of a case-folded term (signals.py).
        cur.execute(
            "INSERT INTO episteme.faers_event_quarter "
            "(substance_key, term_id, quarter, reports_suspect, reports_any) VALUES "
            "(1, %(n)s, '2025-01-01', 2, 3), (1, %(n)s, '2025-04-01', 1, 1), "
            "(1, %(la)s, '2025-04-01', 4, 4), (1, %(la)s, '2024-10-01', 9, 9), "
            "(3, %(n)s, '2025-01-01', 7, 7)",
            {"n": terms["Nausea"], "la": terms["Lactic acidosis"]},
        )
    conn.commit()
    return terms


def _quarter_part(res):
    """The claims of the former per-quarter path."""
    return tuple(
        c for c in res.claims if c.provenance.record_key.startswith("faers_moiety_quarter:")
    )


@pytest.mark.pg
@pytest.mark.parametrize("key", [1, 2, 3, 999])
def test_faers_parity_existing_claims(db, key):
    # Empty FAERS tables, or an unknown substance: the same result on both paths.
    new = lookups.faers_counts(db, key, _FROM, _TO, **KW)
    assert_same_fields(new, legacy.faers_counts(db, key, _FROM, _TO, **KW))
    _quarters(db)
    _splits_and_terms(db)
    old = legacy.faers_counts(db, key, _FROM, _TO, **KW)
    new = lookups.faers_counts(db, key, _FROM, _TO, **KW)
    if key == 999:
        assert_same_fields(new, old)
        return
    # The per-quarter claims are the former ones, first and in the same order; the
    # former notes come first, the new notes after them.
    assert new.claims[: len(old.claims)] == _quarter_part(new)
    assert_same_fields(_quarter_part(new), old.claims)
    assert new.coverage.notes[: len(old.coverage.notes)] == old.coverage.notes
    assert new.coverage.missing == old.coverage.missing == ()
    # A quarter with no totals row: same note on both paths.
    gaps = lookups.faers_counts(db, key, date(2025, 2, 1), date(2025, 9, 30), **KW)
    old_gaps = legacy.faers_counts(db, key, date(2025, 2, 1), date(2025, 9, 30), **KW)
    assert gaps.coverage.notes[: len(old_gaps.coverage.notes)] == old_gaps.coverage.notes
    assert "no FAERS data for quarters: 2025Q3" in gaps.coverage.notes


@pytest.mark.pg
def test_faers_split_claims_present(db):
    _quarters(db)
    terms = _splits_and_terms(db)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.reg_release (dataset, release_id, rows, status, is_latest) "
            "VALUES ('openfda_faers', 'faers-old', 1, 'loaded', false), "
            "('openfda_faers', 'faers-2025q4', 1, 'loaded', true)"
        )
    db.commit()
    res = lookups.faers_counts(db, 2, _FROM, _TO, **KW)  # the salt: its moiety, key 1
    keys = [c.provenance.record_key for c in res.claims]
    # Quarter claims, then the outcome split in code order, then the terms by count.
    assert keys == [
        "faers_moiety_quarter:1:2025-01-01",
        "faers_moiety_quarter:1:2025-04-01",
        "faers_moiety_quarter_outcome:1:2025-01-01:DE",
        "faers_moiety_quarter_outcome:1:2025-01-01:HO",
        "faers_moiety_quarter_outcome:1:2025-01-01:none",
        "faers_moiety_quarter_outcome:1:2025-04-01:OT",
        f"faers_event_quarter:1:{terms['Lactic acidosis']}:2025-01-01:2025-04-01",
        f"faers_event_quarter:1:{terms['Nausea']}:2025-01-01:2025-04-01",
    ]
    by_key = {c.provenance.record_key: c for c in res.claims}
    ho = by_key["faers_moiety_quarter_outcome:1:2025-01-01:HO"]
    assert ho.value == "3" and ho.method == "computed"
    assert ho.text.startswith("2025Q1: 3 reports ")
    assert "outcome HO (hospitalisation), serious" in ho.text
    assert ho.text.endswith(f"({lookups.FAERS_LABEL})")
    none = by_key["faers_moiety_quarter_outcome:1:2025-01-01:none"]
    assert none.value == "2" and "not serious" in none.text
    # Terms: summed over the quarters of the interval, quoted as stored.
    nausea = by_key[f"faers_event_quarter:1:{terms['Nausea']}:2025-01-01:2025-04-01"]
    assert nausea.value == "3"
    assert nausea.text.startswith("Nausea: 3 reports ")
    assert "4 in any role, 2025Q1 to 2025Q2" in nausea.text
    lactic = by_key[f"faers_event_quarter:1:{terms['Lactic acidosis']}:2025-01-01:2025-04-01"]
    assert lactic.value == "4"  # 2024Q4 is outside the interval
    for c in res.claims:
        assert c.provenance.source == "openfda_faers"
        assert c.provenance.release == "faers-2025q4"
        assert c.provenance.jurisdiction == "US"
        assert c.confidence == 1.0
    assert res.coverage.matched == 8
    assert lookups.FAERS_OUTCOME_NOTE in res.coverage.notes
    assert "per report" in lookups.FAERS_OUTCOME_NOTE
    assert lookups.FAERS_TERM_NOTE in res.coverage.notes
    # Every row carries the licence of the FAERS subset.
    raw = _rows(
        db,
        "SELECT kind, licence FROM episteme_api_internal.faers_counts(%s, %s, %s)",
        (2, _FROM, _TO),
    )
    assert {lic for kind, lic in raw if kind == "claim"} == {"pending"}


@pytest.mark.pg
def test_faers_split_not_built_is_noted(db):
    _quarters(db)
    res = lookups.faers_counts(db, 1, _FROM, _TO, **KW)
    assert [c.value for c in res.claims] == ["5", "6"]
    assert lookups.FAERS_SPLIT_GAP in res.coverage.notes
    assert res.coverage.missing == ()


@pytest.mark.pg
def test_faers_missing_row_has_the_faers_licence(db):
    rows = _rows(
        db,
        "SELECT kind, source, licence FROM episteme_api_internal.faers_counts(%s, %s, %s)",
        (1, _FROM, _TO),
    )
    assert ("missing", "faers", "pending") in rows


@pytest.mark.pg
def test_public_faers_counts_caps(db):
    limit = api_result_limits()["faers"][0]
    _quarters(db)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.faers_term (term, term_norm) "
            "SELECT 'Term ' || i, 'term ' || i FROM generate_series(1, %s) AS i",
            (limit + 10,),
        )
        cur.execute(
            "INSERT INTO episteme.faers_event_quarter "
            "(substance_key, term_id, quarter, reports_suspect, reports_any) "
            "SELECT 1, term_id, '2025-01-01', 1, 1 FROM episteme.faers_term"
        )
    db.commit()
    internal = _rows(
        db, "SELECT * FROM episteme_api_internal.faers_counts(1, %s, %s)", (_FROM, _TO)
    )
    public = _rows(db, "SELECT * FROM episteme_api.faers_counts(1, %s, %s)", (_FROM, _TO))
    internal_claims = [r for r in internal if r[0] == "claim"]
    total = len(internal_claims)
    assert total == limit + 12  # two quarter claims and the terms
    assert [r for r in public if r[0] == "claim"] == internal_claims[:limit]
    assert public[-1][3] == f"truncated: {limit} of {total}"
