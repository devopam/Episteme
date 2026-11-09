"""FAERS signal tables, the entry bridge and recalled-lot matching.

The unmarked tests are pure. The pg-marked tests run against ``episteme_test`` only. The
schema is built once for the module and each test clears the data. Most tests seed the
live parents with INSERTs and call ``rebuild_signals`` with no generation, which only renames
the signal tables. One test runs whole generation swaps.
"""

import json
import os
import re
import zipfile
from datetime import date
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from episteme.data.regulatory import load_reg
from episteme.data.regulatory.faers import generation as fg
from episteme.data.regulatory.faers import load as fl
from episteme.data.regulatory.faers import signals as sg
from episteme.data.regulatory.recall_lots import build_recall_lots, lot_matches

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "src/episteme/data/db/migrations"
Q1_2020, Q2_2020 = date(2020, 1, 1), date(2020, 4, 1)
GEN = "g" + "c" * 10


# ---------------------------------------------------------------------------
# pure
# ---------------------------------------------------------------------------
def test_signal_table_names_fit_a_postgres_identifier():
    for table in fg.SIGNAL_TABLES:
        assert len(fg.old_table(table, GEN)) <= 58  # "_pkey" follows


def test_the_old_pattern_finds_old_signal_tables():
    assert re.match(fg._OLD_RE, f"faers_pair_quarter_old_{GEN}")
    assert re.match(fg._OLD_RE, f"faers_report_old_{GEN}")
    assert not re.match(fg._OLD_RE, f"faers_report_old_{GEN}_y2020")


def test_a_lock_failure_on_the_signal_rename_is_a_swap_error(monkeypatch):
    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, query, params=None):
            pass

    class Conn:
        def cursor(self):
            return Cur()

    def locked(cur, token):
        raise psycopg.errors.LockNotAvailable("locked")

    monkeypatch.setattr(fg, "_check_tables", lambda *a: None)
    monkeypatch.setattr(fg, "_swap_signals", locked)
    monkeypatch.setattr(fg.audit_trail, "rollback", lambda conn: None)
    with pytest.raises(fg.GenerationSwapError, match="old signal tables still serve"):
        fg.swap_live_signals(Conn(), GEN, {}, None)


def test_entry_candidates_rank_annotation_then_substance_then_product():
    annotation = json.dumps({"substance_name": ["X"], "unii": ["U1", "U2"]})
    out = sg.entry_candidates(9, "Brand", "Alpha\\Beta; Gamma", annotation)
    assert (9, 1, "unii", "U1") in out and (9, 1, "unii", "U2") in out
    assert {c[3] for c in out if c[1] == 2} == {"alpha", "beta", "gamma"}
    assert [c[3] for c in out if c[1] == 3] == ["brand"]


def test_entry_candidates_survive_bad_annotation_and_empty_fields():
    assert sg.entry_candidates(1, None, None, "not json") == []
    assert sg.entry_candidates(1, None, None, None) == []


# ---------------------------------------------------------------------------
# pg (run by the controller, not in the cloud sandbox)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def _schema():
    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN not set")
    if conninfo_to_dict(dsn).get("dbname") != "episteme_test":
        pytest.fail("TEST_PG_DSN does not target episteme_test; refusing to DROP SCHEMA")
    conn = psycopg.connect(dsn, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
            cur.execute((REPO_ROOT / "src/episteme/data/db/extensions.sql").read_text("utf-8"))
            cur.execute((REPO_ROOT / "src/episteme/data/db/schema.sql").read_text("utf-8"))
            for name in ("0002_container_and_book_parts.sql", "0010_regulatory_sources.sql"):
                cur.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


def _generation_relations(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'episteme' AND c.relkind IN ('r', 'p') AND c.relname ~ %s "
            "ORDER BY 1",
            (r"^faers_.*_g[0-9a-f]{10}$",),
        )
        return [r[0] for r in cur.fetchall()]


@pytest.fixture
def db(_schema, pg_conn, clean_substance, monkeypatch, tmp_path):
    monkeypatch.setenv("EPISTEME_ACTOR", "test")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path / "audit_root"))
    from episteme.config import get_settings

    get_settings.cache_clear()
    with pg_conn.cursor() as cur:
        for name in _generation_relations(pg_conn):
            cur.execute(f"DROP TABLE IF EXISTS episteme.{name} CASCADE")
        cur.execute(
            "TRUNCATE episteme.faers_report, episteme.faers_drug, episteme.faers_reaction, "
            "episteme.faers_drug_entry, episteme.faers_term, episteme.faers_entry_substance, "
            "episteme.substance, episteme.substance_xref, episteme.recall_lot, "
            "episteme.reg_release, episteme.reg_record, "
            + ", ".join(f"episteme.{t}" for t in fg.SIGNAL_TABLES)
        )
        cur.execute("COMMENT ON TABLE episteme.faers_report IS NULL")
    pg_conn.commit()
    yield pg_conn
    pg_conn.rollback()
    get_settings.cache_clear()


def _all(conn, query, params=None):
    """All rows of a read; ends the read transaction, so that no lock stays behind."""
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.rollback()
    return rows


def _substance(conn, name, moiety=None, unii=None):
    """A substance row, with its ``name`` and ``unii`` xref rows. Returns the key."""
    unii = unii or f"U-{name}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.substance (unii, preferred_name, moiety_key) "
            "VALUES (%s, %s, %s) RETURNING substance_key",
            (unii, name, moiety),
        )
        key = cur.fetchone()[0]
        if moiety is None:
            cur.execute(
                "UPDATE episteme.substance SET moiety_key = substance_key WHERE substance_key = %s",
                (key,),
            )
        for id_type, value in (("name", name.casefold()), ("unii", unii)):
            cur.execute(
                "INSERT INTO episteme.substance_xref "
                "(substance_key, id_type, id_value, method, confidence, source) "
                "VALUES (%s, %s, %s, 'exact_code', 1.0, 'test')",
                (key, id_type, value),
            )
    conn.commit()
    return key


def _entry(conn, *keys):
    """A dictionary entry bridged to ``keys``. Returns its id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.faers_drug_entry (product_name) VALUES (%s) "
            "RETURNING drug_entry_id",
            (f"entry-{'-'.join(map(str, keys))}",),
        )
        entry = cur.fetchone()[0]
        for key in keys:
            cur.execute(
                "INSERT INTO episteme.faers_entry_substance VALUES (%s, %s, 'manual')",
                (entry, key),
            )
    conn.commit()
    return entry


def _report(conn, report_id, day, drugs=(), terms=(), gen=""):
    """One report. ``drugs``: ``(entry, role, batch_norm)``. ``terms``: term ids. ``gen``: the
    suffix of a generation's tables, such as ``_gccc``."""
    year = day.year if day else None
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO episteme.faers_report{gen} (safetyreportid, receive_year, receive_date, "
            "source_file) VALUES (%s, %s, %s, 'f')",
            (report_id, year, day),
        )
        for seq, (entry, role, batch) in enumerate(drugs, start=1):
            cur.execute(
                f"INSERT INTO episteme.faers_drug{gen} (safetyreportid, seq, receive_year, role, "
                "drug_entry_id, batch_number, batch_norm, source_file) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'f')",
                (report_id, seq, year, role, entry, batch, batch),
            )
        for seq, term in enumerate(terms, start=1):
            cur.execute(
                f"INSERT INTO episteme.faers_reaction{gen} (safetyreportid, seq, receive_year, "
                "term_id, source_file) VALUES (%s, %s, %s, %s, 'f')",
                (report_id, seq, year, term),
            )
    conn.commit()


def _term(conn, text):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO episteme.faers_term (term) VALUES (%s) RETURNING term_id", (text,))
        term = cur.fetchone()[0]
    conn.commit()
    return term


def _rebuild(conn, cap=30):
    return sg.rebuild_signals(conn, pair_drug_cap=cap)


def _pairs(conn):
    return _all(
        conn,
        "SELECT substance_key_a, substance_key_b, quarter, reports_suspect, reports_any "
        "FROM episteme.faers_pair_quarter ORDER BY 1, 2, 3",
    )


@pytest.mark.pg
def test_pairs_are_stored_in_both_directions(db):
    a, b = _substance(db, "alpha"), _substance(db, "beta")
    ea, eb = _entry(db, a), _entry(db, b)
    _report(db, "1", date(2020, 2, 1), [(ea, "suspect", None), (eb, "suspect", None)])
    _rebuild(db)
    assert _pairs(db) == sorted([(a, b, Q1_2020, 1, 1), (b, a, Q1_2020, 1, 1)])


@pytest.mark.pg
def test_a_report_counts_in_the_quarter_of_its_first_receipt(db):
    a, b = _substance(db, "alpha"), _substance(db, "beta")
    ea, eb = _entry(db, a), _entry(db, b)
    _report(db, "1", date(2020, 3, 31), [(ea, "suspect", None), (eb, "concomitant", None)])
    _report(db, "2", date(2020, 4, 1), [(ea, "suspect", None), (eb, "concomitant", None)])
    _rebuild(db)
    assert {r[2] for r in _pairs(db)} == {Q1_2020, Q2_2020}
    assert _all(db, "SELECT quarter, reports FROM episteme.faers_quarter_totals ORDER BY 1") == [
        (Q1_2020, 1),
        (Q2_2020, 1),
    ]


@pytest.mark.pg
def test_salt_and_base_count_as_one_moiety(db):
    base = _substance(db, "metformin")
    salt = _substance(db, "metformin hydrochloride", moiety=base)
    other = _substance(db, "beta")
    e_base, e_salt, e_other = _entry(db, base), _entry(db, salt), _entry(db, other)
    # One report lists salt and base: one moiety, no pair with itself.
    _report(db, "1", date(2020, 2, 1), [(e_base, "suspect", None), (e_salt, "suspect", None)])
    _report(db, "2", date(2020, 2, 1), [(e_salt, "suspect", None), (e_other, "suspect", None)])
    _report(db, "3", date(2020, 2, 1), [(e_base, "suspect", None), (e_other, "suspect", None)])
    _rebuild(db)
    assert _pairs(db) == sorted([(base, other, Q1_2020, 2, 2), (other, base, Q1_2020, 2, 2)])
    term = _term(db, "Nausea")
    _report(
        db,
        "4",
        date(2020, 2, 1),
        [(e_base, "suspect", None), (e_salt, "suspect", None)],
        [term],
    )
    _rebuild(db)
    assert _all(db, "SELECT substance_key, reports_any FROM episteme.faers_event_quarter") == [
        (base, 1)
    ]


@pytest.mark.pg
def test_suspect_and_any_role_counts_differ_as_expected(db):
    a, b, c = _substance(db, "alpha"), _substance(db, "beta"), _substance(db, "gamma")
    ea, eb, ec = _entry(db, a), _entry(db, b), _entry(db, c)
    term = _term(db, "Nausea")
    # a suspect; b concomitant; c interacting: interacting counts in "any" only.
    _report(
        db,
        "1",
        date(2020, 2, 1),
        [(ea, "suspect", None), (eb, "concomitant", None), (ec, "interacting", None)],
        [term],
    )
    _rebuild(db)
    pairs = {(r[0], r[1]): (r[3], r[4]) for r in _pairs(db)}
    assert pairs[(a, b)] == (1, 1)  # a is suspect
    assert pairs[(b, a)] == (0, 1)  # b is not
    assert pairs[(c, a)] == (0, 1)
    rows = _all(
        db, "SELECT substance_key, reports_suspect, reports_any FROM episteme.faers_event_quarter"
    )
    assert {r[0]: (r[1], r[2]) for r in rows} == {a: (1, 1), b: (0, 1), c: (0, 1)}


@pytest.mark.pg
def test_report_over_drug_cap_is_left_out_and_counted(db):
    keys = [_substance(db, f"sub{i}") for i in range(4)]
    entries = [_entry(db, k) for k in keys]
    term = _term(db, "Nausea")
    _report(db, "big", date(2020, 2, 1), [(e, "suspect", None) for e in entries], [term])
    _report(db, "small", date(2020, 2, 1), [(e, "suspect", None) for e in entries[:3]])
    out = _rebuild(db, cap=3)
    assert out["reports_over_pair_cap"] == 1
    assert _all(db, "SELECT reports, reports_over_pair_cap FROM episteme.faers_quarter_totals") == [
        (2, 1)
    ]
    assert {r[4] for r in _pairs(db)} == {1}  # the pairs come from the small report only
    assert len(_pairs(db)) == 6
    # Only the pairs leave it out.
    assert len(_all(db, "SELECT 1 FROM episteme.faers_event_quarter")) == 4


@pytest.mark.pg
def test_a_report_with_no_date_is_counted_and_left_out(db):
    a = _substance(db, "alpha")
    ea = _entry(db, a)
    _report(db, "dated", date(2020, 2, 1), [(ea, "suspect", "LOT1")])
    _report(db, "undated", None, [(ea, "suspect", "LOT1")])
    out = _rebuild(db)
    assert out["reports"] == 1 and out["reports_no_date"] == 1
    assert _all(db, "SELECT reports FROM episteme.faers_quarter_totals") == [(1,)]
    assert _all(db, "SELECT reports FROM episteme.faers_batch_quarter") == [(1,)]


@pytest.mark.pg
def test_batch_coverage_counts_entries_with_a_batch(db):
    a = _substance(db, "alpha")
    ea = _entry(db, a)
    _report(db, "1", date(2020, 2, 1), [(ea, "suspect", "LOT1"), (ea, "suspect", "LOT1")])
    _report(db, "2", date(2020, 2, 1), [(ea, "suspect", "LOT2")])
    _report(db, "3", date(2020, 2, 1), [(ea, "suspect", "UNKNOWN")])
    _report(db, "4", date(2020, 2, 1), [(ea, "suspect", None)])
    out = _rebuild(db)
    assert _all(
        db,
        "SELECT substance_key, quarter, drug_rows, with_batch FROM episteme.faers_batch_coverage",
    ) == [(a, Q1_2020, 5, 3)]
    assert _all(db, "SELECT batch_norm, reports FROM episteme.faers_batch_quarter ORDER BY 1") == [
        ("LOT1", 1),
        ("LOT2", 1),
    ]
    assert out["placeholder_batches"] == 1 and out["real_batches"] == 3


@pytest.mark.pg
def test_a_term_in_two_cases_is_one_event_on_the_lowest_id(db):
    a = _substance(db, "alpha")
    ea = _entry(db, a)
    upper, lower = _term(db, "NAUSEA"), _term(db, "Nausea")
    _report(db, "2004", date(2020, 2, 1), [(ea, "suspect", None)], [upper])
    _report(db, "2026", date(2020, 2, 2), [(ea, "suspect", None)], [lower])
    _report(db, "both", date(2020, 2, 3), [(ea, "suspect", None)], [upper, lower])
    _rebuild(db)
    assert _all(db, "SELECT term_id, reports_any FROM episteme.faers_event_quarter") == [
        (min(upper, lower), 3)
    ]
    # The published text stays.
    assert _all(db, "SELECT term, term_norm FROM episteme.faers_term ORDER BY term_id") == [
        ("NAUSEA", "nausea"),
        ("Nausea", "nausea"),
    ]


@pytest.mark.pg
def test_denominators_give_a_hand_worked_two_by_two(db):
    m, x = _substance(db, "alpha"), _substance(db, "gamma")
    em, ex = _entry(db, m), _entry(db, x)
    t, u = _term(db, "Nausea"), _term(db, "Rash")
    day = date(2020, 2, 1)
    _report(db, "1", day, [(em, "suspect", None)], [t])
    _report(db, "2", day, [(em, "suspect", None)], [t])
    _report(db, "3", day, [(em, "concomitant", None)], [u])
    _report(db, "4", day, [(ex, "suspect", None)], [t])
    _report(db, "5", day, [(ex, "suspect", None)], [u])
    _report(db, "6", day, [(ex, "suspect", None)], [u])
    _report(db, "7", day, [(_raw_entry(db, "mystery"), "suspect", None)], [])  # no drug, no term
    out = _rebuild(db)
    a = _all(
        db,
        "SELECT reports_any FROM episteme.faers_event_quarter "
        "WHERE substance_key = %s AND term_id = %s",
        (m, t),
    )[0][0]
    drug_total, drug_suspect = _all(
        db,
        "SELECT reports_any, reports_suspect FROM episteme.faers_moiety_quarter "
        "WHERE substance_key = %s AND quarter = %s",
        (m, Q1_2020),
    )[0]
    term_total = _all(
        db, "SELECT reports FROM episteme.faers_term_quarter WHERE term_id = %s", (t,)
    )[0][0]
    reports, listed, with_reaction = _all(
        db,
        "SELECT reports, reports_listed, reports_with_reaction FROM episteme.faers_quarter_totals",
    )[0]
    assert (reports, listed, with_reaction) == (7, 6, 6)
    assert out["reports_listed"] == 6 and out["reports_with_reaction"] == 6
    b, c = drug_total - a, term_total - a
    d = listed - a - b - c
    assert (a, b, c, d) == (2, 1, 1, 2)
    assert drug_suspect == 2
    assert (a / (a + b)) / (c / (c + d)) == pytest.approx(2.0)  # PRR
    assert (a * d) / (b * c) == pytest.approx(4.0)  # ROR


@pytest.mark.pg
def test_a_report_set_over_two_year_partitions(db):
    a, b = _substance(db, "alpha"), _substance(db, "gamma")
    ea, eb = _entry(db, a), _entry(db, b)
    term = _term(db, "Nausea")
    both = [(ea, "suspect", "LOT1"), (eb, "concomitant", None)]
    _report(db, "1", date(2019, 12, 31), both, [term])
    _report(db, "2", date(2020, 1, 1), both, [term])
    _report(db, "3", date(2020, 2, 1), both, [term])
    out = _rebuild(db)
    assert out["reports"] == 3 and out["pair_rows"] == 4
    q4_2019 = date(2019, 10, 1)
    assert _all(db, "SELECT quarter, reports FROM episteme.faers_quarter_totals ORDER BY 1") == [
        (q4_2019, 1),
        (Q1_2020, 2),
    ]
    assert _all(
        db,
        "SELECT quarter, reports_any FROM episteme.faers_pair_quarter "
        "WHERE substance_key_a = %s ORDER BY 1",
        (a,),
    ) == [(q4_2019, 1), (Q1_2020, 2)]
    assert _all(db, "SELECT quarter, reports FROM episteme.faers_batch_quarter ORDER BY 1") == [
        (q4_2019, 1),
        (Q1_2020, 2),
    ]
    assert _all(db, "SELECT quarter, reports FROM episteme.faers_term_quarter ORDER BY 1") == [
        (q4_2019, 1),
        (Q1_2020, 2),
    ]


@pytest.mark.pg
def test_a_lock_on_a_signal_table_stops_the_rename_and_the_old_ones_serve(db, monkeypatch):
    monkeypatch.setenv("EPISTEME_FAERS_LOCK_TIMEOUT_S", "1")
    from episteme.config import get_settings

    get_settings.cache_clear()
    a, b = _substance(db, "alpha"), _substance(db, "gamma")
    ea, eb = _entry(db, a), _entry(db, b)
    _report(db, "1", date(2020, 2, 1), [(ea, "suspect", None), (eb, "suspect", None)])
    _rebuild(db)
    _report(db, "2", date(2020, 5, 1), [(ea, "suspect", None), (eb, "suspect", None)])
    reader = psycopg.connect(os.environ["TEST_PG_DSN"], autocommit=False)
    try:
        with reader.cursor() as cur:
            cur.execute("SELECT count(*) FROM episteme.faers_pair_quarter")
        with pytest.raises(fg.GenerationSwapError, match="old signal tables still serve"):
            _rebuild(db)
        db.rollback()
    finally:
        reader.rollback()
        reader.close()
    assert {r[2] for r in _pairs(db)} == {Q1_2020}
    _rebuild(db)  # drops what the stopped rebuild left
    assert {r[2] for r in _pairs(db)} == {Q1_2020, Q2_2020}
    assert _generation_relations(db) == []
    db.rollback()


@pytest.mark.pg
def test_a_live_signal_table_with_other_privileges_is_refused_before_the_copy(
    db, tmp_path, monkeypatch
):
    from episteme.data.regulatory.faers import zip_load as fz

    raw = tmp_path / "raw"
    with db.cursor() as cur:
        cur.execute("GRANT SELECT ON episteme.faers_pair_quarter TO PUBLIC")
    db.commit()
    monkeypatch.setattr(fz, "iter_reports", lambda _p: pytest.fail("the COPY started"))
    try:
        with pytest.raises(fl.GenerationError, match="owner or privileges"):
            _run(db, tmp_path, [_zip(raw, "2020q1", [_zip_report("1", ["alpha"], "20200201")])])
    finally:
        db.rollback()
        with db.cursor() as cur:
            cur.execute("REVOKE SELECT ON episteme.faers_pair_quarter FROM PUBLIC")
        db.commit()


@pytest.mark.pg
def test_unbridged_drug_rows_are_counted(db):
    a = _substance(db, "alpha")
    ea = _entry(db, a)
    loose = _raw_entry(db, "mystery")
    _report(db, "1", date(2020, 2, 1), [(ea, "suspect", None), (loose, "suspect", None)])
    out = _rebuild(db)
    assert out["drug_rows"] == 2 and out["drug_rows_unbridged"] == 1


def _raw_entry(conn, product=None, active=None, annotation=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.faers_drug_entry (product_name, active_substance, annotation) "
            "VALUES (%s, %s, %s) RETURNING drug_entry_id",
            (product, active, annotation),
        )
        entry = cur.fetchone()[0]
    conn.commit()
    return entry


def _bridge_rows(conn, entry):
    return _all(
        conn,
        "SELECT substance_key, method FROM episteme.faers_entry_substance "
        "WHERE drug_entry_id = %s ORDER BY 1",
        (entry,),
    )


@pytest.mark.pg
def test_bridge_prefers_annotation_then_substance_then_product(db):
    a = _substance(db, "alpha", unii="UA")
    b = _substance(db, "beta", unii="UB")
    annotated = _raw_entry(db, "beta", "beta", json.dumps({"unii": ["UA"]}))
    named = _raw_entry(db, "alpha", "Beta")
    product = _raw_entry(db, "ALPHA", "no such substance")
    combo = _raw_entry(db, None, "Alpha\\BETA")
    none = _raw_entry(db, "nothing", "nothing")
    out = sg.bridge_entries(db)
    assert _bridge_rows(db, annotated) == [(a, "annotation")]
    assert _bridge_rows(db, named) == [(b, "active_substance")]
    assert _bridge_rows(db, product) == [(a, "product_name")]
    assert _bridge_rows(db, combo) == [(a, "active_substance"), (b, "active_substance")]
    assert _bridge_rows(db, none) == []
    assert out == {
        "entries": 5,
        "annotation": 1,
        "active_substance": 2,
        "product_name": 1,
        "unmatched": 1,
    }


@pytest.mark.pg
def test_bridge_reads_only_new_entries_unless_told_to_redo(db):
    a = _substance(db, "alpha")
    b = _substance(db, "beta")
    entry = _raw_entry(db, None, "alpha")
    assert sg.bridge_entries(db)["active_substance"] == 1
    assert sg.bridge_entries(db)["entries"] == 0
    with db.cursor() as cur:  # the name moves to another substance
        cur.execute(
            "UPDATE episteme.substance_xref SET substance_key = %s "
            "WHERE id_type = 'name' AND id_value = 'alpha'",
            (b,),
        )
    db.commit()
    assert sg.bridge_entries(db)["entries"] == 0
    assert _bridge_rows(db, entry) == [(a, "active_substance")]
    assert sg.bridge_entries(db, redo=True)["entries"] == 1
    assert _bridge_rows(db, entry) == [(b, "active_substance")]


@pytest.mark.pg
def test_an_unmatched_entry_is_tried_again_after_the_xref_grows(db):
    entry = _raw_entry(db, None, "alpha")
    assert sg.bridge_entries(db)["unmatched"] == 1
    a = _substance(db, "alpha")
    assert sg.bridge_entries(db)["active_substance"] == 1
    assert _bridge_rows(db, entry) == [(a, "active_substance")]


def _zip(raw, quarter, reports):
    path = raw / "drug" / "event" / quarter / "drug-event-0001-of-0001.json.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(path.name.removesuffix(".zip"), json.dumps({"meta": {}, "results": reports}))
    return path


def _zip_report(report_id, products, received):
    return {
        "safetyreportid": report_id,
        "receivedate": received,
        "serious": "1",
        "patient": {
            "drug": [{"medicinalproduct": p, "drugcharacterization": "1"} for p in products],
            "reaction": [{"reactionmeddrapt": "Nausea"}],
        },
    }


def _run(conn, tmp_path, zips, **kw):
    return fl.load_generation(
        conn, zips, raw_dir=tmp_path / "raw", processed_dir=tmp_path / "proc", run_id="t", **kw
    )


@pytest.mark.pg
def test_signals_swap_with_the_generation_and_the_old_ones_go(db, tmp_path, monkeypatch):
    a, b = _substance(db, "alpha"), _substance(db, "beta")
    raw = tmp_path / "raw"
    first = _zip(raw, "2020q1", [_zip_report("1", ["alpha", "beta"], "20200201")])
    _run(db, tmp_path, [first], post_load=fl.build_signals)
    assert _pairs(db) == sorted([(a, b, Q1_2020, 1, 1), (b, a, Q1_2020, 1, 1)])

    reader = psycopg.connect(os.environ["TEST_PG_DSN"], autocommit=True)
    seen = {}
    real_swap = fl._swap

    def watched(conn, generation, *args, **kw):
        seen["before"] = _all(reader, "SELECT quarter FROM episteme.faers_pair_quarter")
        out = real_swap(conn, generation, *args, **kw)
        seen["after"] = _all(reader, "SELECT quarter FROM episteme.faers_pair_quarter")
        seen["reports"] = _all(reader, "SELECT safetyreportid FROM episteme.faers_report")
        return out

    monkeypatch.setattr(fl, "_swap", watched)
    try:
        second = _zip(raw, "2020q2", [_zip_report("2", ["alpha", "beta"], "20200501")])
        out = _run(db, tmp_path, [second], post_load=fl.build_signals)
    finally:
        reader.close()
    monkeypatch.setattr(fl, "_swap", real_swap)
    assert seen["before"] == [(Q1_2020,), (Q1_2020,)]
    assert seen["after"] == [(Q2_2020,), (Q2_2020,)]
    assert seen["reports"] == [("2",)]
    assert out["post_load"]["signals"]["pair_rows"] == 2
    assert _generation_relations(db) == []
    db.rollback()
    # The primary key came with the new table.
    assert _all(
        db,
        "SELECT count(*) FROM pg_index i WHERE i.indisprimary "
        "AND i.indrelid = 'episteme.faers_pair_quarter'::regclass",
    ) == [(1,)]

    # A generation with no signals of its own must not sit beside the old ones.
    third = _zip(raw, "2020q3", [_zip_report("3", ["alpha"], "20200801")])
    _run(db, tmp_path, [third])
    assert _pairs(db) == []


@pytest.mark.pg
def test_a_stale_generation_takes_its_signal_tables_with_it(db):
    fg._create_generation(db, GEN, "a" * 64)
    sg.rebuild_signals(db, pair_drug_cap=30, generation=GEN)
    assert f"faers_pair_quarter_{GEN}" in _generation_relations(db)
    db.rollback()
    fg._drop_generation(db, GEN)
    assert _generation_relations(db) == []
    db.rollback()


@pytest.mark.pg
def test_a_failed_build_is_cleaned_by_the_next_build(db, monkeypatch):
    fg._create_generation(db, GEN, "a" * 64)
    real = sg._finish_tables

    def fail(*args, **kw):
        raise RuntimeError("stop")

    monkeypatch.setattr(sg, "_finish_tables", fail)
    with pytest.raises(RuntimeError, match="stop"):
        sg.rebuild_signals(db, pair_drug_cap=30, generation=GEN)
    db.rollback()
    monkeypatch.setattr(sg, "_finish_tables", real)
    out = sg.rebuild_signals(db, pair_drug_cap=30, generation=GEN)
    assert out["reports"] == 0  # the generation's own tables are empty
    fg._drop_generation(db, GEN)


def _state_rows(conn, kind):
    return _all(
        conn, f"SELECT key FROM episteme.{fg.state_table(GEN)} WHERE kind = %s ORDER BY 1", (kind,)
    )


@pytest.mark.pg
def test_a_failure_on_the_last_partition_keeps_the_finished_ones(db, monkeypatch):
    a, b = _substance(db, "alpha"), _substance(db, "gamma")
    ea, eb = _entry(db, a), _entry(db, b)
    fg._create_generation(db, GEN, "a" * 64)
    both = [(ea, "suspect", None), (eb, "suspect", None)]
    _report(db, "1", date(2020, 2, 1), both, gen=f"_{GEN}")
    _report(db, "2", date(2030, 2, 1), both, gen=f"_{GEN}")
    real = sg._build_partition
    calls = []

    def fail_on_last(cur, leaves, new, cap):
        name = leaves["faers_report"].as_string(cur)
        calls.append(name)
        if name.endswith('_y2030"'):
            raise RuntimeError("stop")
        return real(cur, leaves, new, cap)

    monkeypatch.setattr(sg, "_build_partition", fail_on_last)
    with pytest.raises(RuntimeError, match="stop"):
        sg.rebuild_signals(db, pair_drug_cap=30, generation=GEN)
    db.rollback()
    finished = len(calls) - 1
    assert len(_state_rows(db, "signals")) == finished
    monkeypatch.setattr(sg, "_create_tables", lambda *a: pytest.fail("the tables were re-created"))

    calls.clear()

    def only_the_last(cur, leaves, new, cap):
        calls.append(leaves["faers_report"].as_string(cur))
        return real(cur, leaves, new, cap)

    monkeypatch.setattr(sg, "_build_partition", only_the_last)
    out = sg.rebuild_signals(db, pair_drug_cap=30, generation=GEN)
    assert len(calls) == 1 and calls[0].endswith('_y2030"')
    assert out["reports"] == 2 and out["pair_rows"] == 4  # the finished partition still counts
    assert _all(db, f"SELECT count(*) FROM episteme.faers_pair_quarter_{GEN}") == [(4,)]
    assert _state_rows(db, "phase") != []


@pytest.mark.pg
def test_a_failure_in_the_key_step_repeats_only_the_missing_keys(db, monkeypatch):
    fg._create_generation(db, GEN, "a" * 64)
    real = sg._add_primary_key
    added = []

    def fail_on_third(conn, table, name):
        if len(added) == 2:
            raise RuntimeError("stop")
        added.append(table)
        real(conn, table, name)

    monkeypatch.setattr(sg, "_add_primary_key", fail_on_third)
    with pytest.raises(RuntimeError, match="stop"):
        sg.rebuild_signals(db, pair_drug_cap=30, generation=GEN)
    db.rollback()
    first = list(added)
    added.clear()
    monkeypatch.setattr(sg, "_add_primary_key", lambda c, t, n: (added.append(t), real(c, t, n)))
    monkeypatch.setattr(
        sg, "_build_partition", lambda *a: pytest.fail("a finished partition was built again")
    )
    monkeypatch.setattr(sg, "_create_tables", lambda *a: pytest.fail("the tables were re-created"))
    sg.rebuild_signals(db, pair_drug_cap=30, generation=GEN)
    assert not set(first) & set(added)
    assert set(first) | set(added) == set(fg.SIGNAL_TABLES)
    added.clear()
    # A finished build returns at once.
    assert sg.rebuild_signals(db, pair_drug_cap=30, generation=GEN)["pair_drug_cap"] == 30
    assert added == []


@pytest.mark.pg
def test_another_cap_restarts_the_build(db):
    a, b = _substance(db, "alpha"), _substance(db, "gamma")
    both = [(_entry(db, a), "suspect", None), (_entry(db, b), "suspect", None)]
    fg._create_generation(db, GEN, "a" * 64)
    _report(db, "1", date(2020, 2, 1), both, gen=f"_{GEN}")
    assert sg.rebuild_signals(db, pair_drug_cap=30, generation=GEN)["pair_rows"] == 2
    assert sg.rebuild_signals(db, pair_drug_cap=1, generation=GEN)["pair_rows"] == 0
    assert _all(db, f"SELECT count(*) FROM episteme.faers_pair_quarter_{GEN}") == [(0,)]


@pytest.mark.pg
def test_half_built_signals_never_swap_and_a_rerun_repeats_no_finished_work(
    db, tmp_path, monkeypatch
):
    a, b = _substance(db, "alpha"), _substance(db, "gamma")
    raw = tmp_path / "raw"
    z = _zip(raw, "2020q1", [_zip_report("1", ["alpha", "gamma"], "20200201")])
    real_key, real_partition, real_bridge = (
        sg._add_primary_key,
        sg._build_partition,
        fl.bridge_entries,
    )

    def fail(*args):
        raise RuntimeError("stop")

    monkeypatch.setattr(sg, "_add_primary_key", fail)
    with pytest.raises(RuntimeError, match="stop"):
        _run(db, tmp_path, [z], post_load=fl.build_signals)
    db.rollback()
    monkeypatch.setattr(sg, "_add_primary_key", real_key)
    monkeypatch.setattr(sg, "_build_partition", lambda *a: pytest.fail("built again"))
    monkeypatch.setattr(fl, "bridge_entries", lambda conn, **kw: pytest.fail("bridged again"))
    out = _run(db, tmp_path, [z], post_load=fl.build_signals)
    assert out["status"] == "swapped" and out["post_load"]["signals"]["pair_rows"] == 2
    assert out["post_load"]["bridge"]["active_substance"] == 2
    assert _pairs(db) == sorted([(a, b, Q1_2020, 1, 1), (b, a, Q1_2020, 1, 1)])
    monkeypatch.setattr(sg, "_build_partition", real_partition)
    monkeypatch.setattr(fl, "bridge_entries", real_bridge)

    # A build that stopped, then a run with no post_load: the half-built tables must not go in.
    z2 = _zip(raw, "2020q2", [_zip_report("2", ["alpha", "gamma"], "20200501")])
    monkeypatch.setattr(sg, "_add_primary_key", fail)
    with pytest.raises(RuntimeError, match="stop"):
        _run(db, tmp_path, [z2], post_load=fl.build_signals)
    db.rollback()
    monkeypatch.setattr(sg, "_add_primary_key", real_key)
    assert _pairs(db) != []
    out = _run(db, tmp_path, [z2])
    assert out["status"] == "swapped"
    assert _pairs(db) == []
    assert _generation_relations(db) == []
    db.rollback()


@pytest.mark.pg
def test_a_forced_run_sees_a_link_that_a_reload_moved(db, tmp_path):
    import functools

    a, b, c = _substance(db, "alpha"), _substance(db, "beta"), _substance(db, "gamma")
    raw = tmp_path / "raw"
    z = _zip(raw, "2020q1", [_zip_report("1", ["alpha", "gamma"], "20200201")])
    _run(db, tmp_path, [z], post_load=fl.build_signals)
    assert _pairs(db) == sorted([(a, c, Q1_2020, 1, 1), (c, a, Q1_2020, 1, 1)])
    with db.cursor() as cur:  # a substance reload moves the name to another substance
        cur.execute(
            "UPDATE episteme.substance_xref SET substance_key = %s "
            "WHERE id_type = 'name' AND id_value = 'alpha'",
            (b,),
        )
    db.commit()
    # What the CLI passes for --force --reason: a fresh generation, every entry re-bridged.
    out = _run(
        db, tmp_path, [z], force=True, post_load=functools.partial(fl.build_signals, redo=True)
    )
    assert out["status"] == "swapped"
    assert _pairs(db) == sorted([(b, c, Q1_2020, 1, 1), (c, b, Q1_2020, 1, 1)])
    db.rollback()


@pytest.mark.pg
def test_signals_that_bridge_nothing_are_not_swapped_in_even_in_a_sample(db, tmp_path):
    _substance(db, "alpha")
    raw = tmp_path / "raw"
    z = _zip(raw, "2020q1", [_zip_report("1", ["nothing", "unknown"], "20200201")])
    with pytest.raises(fl.GenerationError, match="bridge matched no drug entry.*0 rows"):
        _run(db, tmp_path, [z], post_load=fl.build_signals, sample=True, check_signals=True)
    db.rollback()
    assert _all(db, "SELECT count(*) FROM episteme.faers_report") == [(0,)]
    out = _run(
        db,
        tmp_path,
        [z],
        post_load=fl.build_signals,
        override_reason="known gap",
        check_signals=True,
    )
    assert out["status"] == "swapped" and "empty signals" in out["overrides"]
    db.rollback()


@pytest.mark.pg
def test_a_live_rebuild_leaves_no_leftovers(db):
    a = _substance(db, "alpha")
    _report(db, "1", date(2020, 2, 1), [(_entry(db, a), "suspect", "LOT1")])
    _rebuild(db)
    assert _generation_relations(db) == []
    db.rollback()
    assert _all(db, "SELECT reports FROM episteme.faers_batch_quarter") == [(1,)]


def _feed_rows(conn, dataset, release, rows, *, latest=True):
    """``rows``: ``(record_key, substance_names, product_keys, payload)``."""
    with conn.cursor() as cur:
        load_reg._create_partitions(cur, dataset, release)
        for key, names, products, payload in rows:
            cur.execute(
                "INSERT INTO episteme.reg_record (dataset, release_id, record_key, "
                "substance_names, product_keys, payload, source_file) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'f')",
                (dataset, release, key, names, products, json.dumps(payload)),
            )
        cur.execute(
            "INSERT INTO episteme.reg_release (dataset, release_id, status, is_latest) "
            "VALUES (%s, %s, 'ok', %s)",
            (dataset, release, latest),
        )
    conn.commit()


def _recall(number, code_info, description="x", **openfda):
    return {
        "recall_number": number,
        "code_info": code_info,
        "product_description": description,
        "openfda": openfda,
    }


@pytest.mark.pg
def test_lot_match_needs_same_moiety_and_same_lot(db):
    base = _substance(db, "metformin")
    _substance(db, "metformin hydrochloride", moiety=base)
    other = _substance(db, "beta")
    e_base, e_other = _entry(db, base), _entry(db, other)
    _report(db, "1", date(2020, 2, 1), [(e_base, "suspect", "AB123")])
    _report(db, "2", date(2020, 5, 1), [(e_base, "suspect", "AB123")])
    _report(db, "3", date(2020, 2, 1), [(e_base, "suspect", "ZZ999")])  # another lot
    _report(db, "4", date(2020, 2, 1), [(e_other, "suspect", "CD456")])  # another moiety
    _report(db, "5", date(2020, 2, 1), [(e_base, "suspect", None)])  # no batch
    _rebuild(db)
    _feed_rows(
        db,
        "fda_enforcement",
        "r1",
        [
            ("D-1", ["metformin hydrochloride"], None, _recall("D-1", "Lot #: AB-123, Exp 12/25")),
            ("D-2", ["metformin"], None, _recall("D-2", "Lot CD456")),  # CD456 is beta's
            ("D-3", ["metformin"], None, _recall("D-3", "All lots")),
            ("D-4", None, None, _recall("D-4", "Lot AB123")),  # no route to a substance
        ],
    )
    counts = build_recall_lots(db, "r1")
    db.commit()
    assert (counts["recalls"], counts["with_lot"], counts["no_lot"], counts["lots"]) == (4, 3, 1, 3)
    out = lot_matches(db, base)
    db.rollback()
    assert out["release_id"] == "r1"
    assert out["matches"] == [
        {
            "recall_number": "D-1",
            "lot_raw": "AB-123",
            "lot_norm": "AB123",
            "method": "prefix",
            "substance_method": "openfda",
            "reports": 2,
            "first_quarter": Q1_2020,
            "last_quarter": Q2_2020,
        }
    ]
    assert out["batch_coverage"] == {"drug_rows": 4, "with_batch": 3}
    assert out["recalls"] == 3 and out["recalls_no_lot"] == 1
    assert out["recalls_by_method"] == {"openfda": 3}
    assert out["recalls_no_substance"] == 1
    assert out["recall_lots_built"] is True
    assert lot_matches(db, other)["matches"] == []
    db.rollback()


@pytest.mark.pg
def test_no_match_still_reports_the_coverage(db):
    a = _substance(db, "alpha")
    out = lot_matches(db, a)
    db.rollback()
    assert out["release_id"] is None and out["matches"] == []
    assert out["batch_coverage"] == {"drug_rows": 0, "with_batch": 0}
    assert out["recalls"] == 0
    assert out["recall_lots_built"] is False


@pytest.mark.pg
def test_a_release_with_no_recall_lot_rows_says_so(db):
    a = _substance(db, "alpha")
    _feed_rows(db, "fda_enforcement", "r1", [("D-1", ["alpha"], None, _recall("D-1", "Lot AA1"))])
    out = lot_matches(db, a)
    db.rollback()
    assert out["release_id"] == "r1" and out["recalls"] == 0
    assert out["recall_lots_built"] is False
    build_recall_lots(db, "r1")
    db.commit()
    out = lot_matches(db, a)
    db.rollback()
    assert out["recall_lots_built"] is True and out["recalls"] == 1


@pytest.mark.pg
def test_names_with_no_key_fall_through_to_the_next_route(db):
    _substance(db, "alpha")
    _feed_rows(db, "fda_ndc", "n1", [("p1", ["no such name"], ["005912435"], {})])
    _feed_rows(
        db,
        "fda_enforcement",
        "r1",
        [
            # openfda names and the NDC's names have no key, and the text has one.
            (
                "D-1",
                ["unknown thing"],
                None,
                _recall("D-1", "Lot AA1", "NDC 0591-2435-15, Alpha Inj"),
            ),
            # Nothing has a key anywhere: it counts as no substance.
            ("D-2", ["unknown thing"], None, _recall("D-2", "Lot AA2", "NDC 0591-2435-15, Inj")),
        ],
    )
    counts = build_recall_lots(db, "r1")
    db.commit()
    rows = {
        r[0]: (r[1], r[2])
        for r in _all(
            db, "SELECT recall_number, substance_names, substance_method FROM episteme.recall_lot"
        )
    }
    assert rows["D-1"] == (["alpha"], "product_text")
    assert rows["D-2"] == (None, None)
    assert counts["no_substance"] == 1 and counts["by_method"] == {"product_text": 1}


@pytest.mark.pg
def test_a_placeholder_never_matches(db):
    a = _substance(db, "alpha")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.faers_batch_quarter VALUES (%s, 'UNKNOWN', %s, 5)", (a, Q1_2020)
        )
        cur.execute(
            "INSERT INTO episteme.recall_lot (recall_number, release_id, substance_names, "
            "lot_raw, lot_norm, method, substance_method) "
            "VALUES ('D-1', 'r1', ARRAY['alpha'], 'UNKNOWN', 'UNKNOWN', 'prefix', 'openfda')"
        )
    db.commit()
    _feed_rows(db, "fda_enforcement", "r1", [])
    assert lot_matches(db, a)["matches"] == []
    db.rollback()


@pytest.mark.pg
def test_recall_substances_resolve_in_order(db):
    _substance(db, "alpha", unii="UA")
    _substance(db, "ibuprofen lysine")
    _substance(db, "gamma")
    _substance(db, "beta")  # four letters: found through a route, and not in the text
    _substance(db, "abcd")  # shorter than 5: ignored
    _feed_rows(
        db,
        "fda_ndc",
        "n1",
        [
            ("p1", ["beta", "gamma"], ["005912435", "00591243515"], {}),
            ("p2", ["wrong"], ["999990001"], {}),
        ],
    )
    _feed_rows(db, "fda_ndc", "n0", [("p9", ["wrong"], ["005912435"], {})], latest=False)
    _feed_rows(
        db,
        "fda_enforcement",
        "r1",
        [
            # openfda names win over an NDC and over the text.
            ("D-1", ["beta"], None, _recall("D-1", "Lot AA1", "NDC 0591-2435-15 alpha")),
            # openfda UNII alone.
            ("D-2", None, None, _recall("D-2", "Lot AA2", "x", unii=["UA"])),
            # NDC, in the description or the code text; the package digits differ.
            ("D-3", None, None, _recall("D-3", "Lot AA3", "NDC 0591-2435-99, alpha")),
            ("D-4", None, None, _recall("D-4", "NDC 0591-2435-15 Lot AA4", "alpha")),
            # An NDC that the feed lacks falls through to the text.
            ("D-5", None, None, _recall("D-5", "Lot AA5", "NDC 11111-222-33 Alpha Tablets")),
            # Text: the longest name, and several names.
            ("D-6", None, None, _recall("D-6", "Lot AA6", "Ibuprofen Lysine Inj, Gamma, 2 mL")),
            # Nothing. "abcd" is too short, and "alpah" is not fuzzy-matched.
            ("D-7", None, None, _recall("D-7", "Lot AA7", "abcd alpah")),
        ],
    )
    counts = build_recall_lots(db, "r1")
    db.commit()
    rows = {
        r[0]: (r[1], r[2])
        for r in _all(
            db, "SELECT recall_number, substance_names, substance_method FROM episteme.recall_lot"
        )
    }
    assert rows["D-1"] == (["beta"], "openfda")
    assert rows["D-2"] == (["alpha"], "openfda")
    assert rows["D-3"] == (["beta", "gamma"], "ndc")
    assert rows["D-4"] == (["beta", "gamma"], "ndc")
    assert rows["D-5"] == (["alpha"], "product_text")
    assert rows["D-6"] == (["ibuprofen lysine", "gamma"], "product_text")
    assert rows["D-7"] == (None, None)
    assert counts["by_method"] == {"openfda": 2, "ndc": 2, "product_text": 2}
    assert counts["no_substance"] == 1


@pytest.mark.pg
def test_a_match_says_how_strong_its_link_is(db):
    a = _substance(db, "alpha")
    ea = _entry(db, a)
    _report(db, "1", date(2020, 2, 1), [(ea, "suspect", "AA1")])
    _rebuild(db)
    _feed_rows(
        db, "fda_enforcement", "r1", [("D-1", None, None, _recall("D-1", "Lot AA1", "Alpha Inj"))]
    )
    build_recall_lots(db, "r1")
    db.commit()
    out = lot_matches(db, a)
    db.rollback()
    assert out["matches"][0]["substance_method"] == "product_text"
    assert out["recalls_by_method"] == {"product_text": 1}
    assert out["recalls_no_substance"] == 0


@pytest.mark.pg
def test_a_rerun_replaces_the_rows_of_its_release_only(db):
    _feed_rows(db, "fda_enforcement", "r1", [("D-1", ["x"], None, _recall("D-1", "Lot AA1"))])
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.recall_lot (recall_number, release_id, lot_raw, lot_norm, "
            "method) VALUES ('old', 'r0', 'Z1', 'Z1', 'prefix')"
        )
    db.commit()
    build_recall_lots(db, "r1")
    build_recall_lots(db, "r1")
    db.commit()
    assert _all(
        db, "SELECT release_id, count(*) FROM episteme.recall_lot GROUP BY 1 ORDER BY 1"
    ) == [("r0", 1), ("r1", 1)]
