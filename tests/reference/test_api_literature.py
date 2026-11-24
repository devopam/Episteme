"""literature_by_mesh as a SQL function (migration 0015), with the results the former
Python query path gave on these fixtures as fixed expectations (pg: episteme_test only),
and the article_row mapping (non-pg)."""

from __future__ import annotations

import dataclasses

import pytest

from episteme.reference import _api, api_settings
from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance
from episteme.reference.literature import literature

from ._pbrer_db import KW

# The topic tree of the fixture. 'Liver Diseases' C06.552 has two children; 'Decoy'
# C06.5521 only shares the characters of its tree number and is not below it.
_TREE = (
    ("D008107", "C06.552"),  # Liver Diseases
    ("D056486", "C06.552.474"),  # Chemical and Drug Induced Liver Injury
    ("D006505", "C06.552.380"),  # Hepatitis
    ("D007674", "C12.777"),  # Kidney Diseases
    ("D999999", "C06.5521"),  # Decoy
)


# ---------------------------------------------------------------------------
# non-pg
# ---------------------------------------------------------------------------
def _row(**kw):
    base = dict(
        kind="article",
        article_id="pubmed:7",
        source="pubmed",
        title=None,
        year=2021,
        score=1.0,
        relation="Metformin",
        release_id="2025",
        licence="open_metadata",
        note=None,
        source_file="pubmed/x.xml",
    )
    base.update(kw)
    return tuple(base[c] for c in _api.ARTICLE_COLUMNS)


def test_article_rows_map_to_claims_notes_and_missing():
    rows = [
        _row(kind="note", article_id=None, note="a note"),
        _row(),
        _row(article_id="pubmed:8", year=None, score=0.5),
        _row(kind="missing", article_id=None, source="article_mesh"),
    ]
    res = _api.article_rows_to_result(
        rows, describe=lambda r: f"{r['article_id']}|{r['year']}", confidence_basis="basis"
    )
    assert [c.value for c in res.claims] == ["7", "8"]
    first = res.claims[0]
    assert first.text == "pubmed:7|2021"
    assert first.provenance.record_key == "pmid:7"
    assert first.provenance.source == "pubmed"
    assert first.provenance.release == "2025"
    assert first.provenance.source_file == "pubmed/x.xml"
    assert first.method == "fetched" and first.confidence_basis == "basis"
    assert res.claims[1].confidence == 0.5
    assert res.coverage.matched == 2
    assert res.coverage.notes == ("a note",)
    assert res.coverage.missing == ("article_mesh",)


def test_article_rows_reject_bad_rows():
    describe = lambda r: ""  # noqa: E731
    with pytest.raises(ValueError):
        _api.article_rows_to_result([_row(kind="other")], describe=describe, confidence_basis="b")
    with pytest.raises(ValueError):
        _api.article_rows_to_result([("article",)], describe=describe, confidence_basis="b")


# ---------------------------------------------------------------------------
# pg
# ---------------------------------------------------------------------------
@pytest.fixture
def db(ref_db):
    """Metformin (1, with its salt 2) and aspirin (3); the API settings from config;
    MeSH rows, article years and a small MeSH tree."""
    api_settings.sync(ref_db)
    with ref_db.cursor() as cur:
        # schema.sql has no mesh_tree (migration 0006 creates it, with other tables);
        # the same DDL as 0006.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS episteme.mesh_tree ("
            "descriptor_ui text NOT NULL, tree_number text NOT NULL, source_file text)"
        )
        cur.execute("DELETE FROM episteme.mesh_tree")
        cur.executemany(
            "INSERT INTO episteme.mesh_tree (descriptor_ui, tree_number) VALUES (%s, %s)", _TREE
        )
        cur.execute("DELETE FROM episteme.reg_release WHERE dataset = 'pubmed'")
        cur.execute(
            "INSERT INTO episteme.article_mesh "
            "(pmid, descriptor_ui, descriptor_name, major_topic, qualifiers, source_file) VALUES "
            "('100', 'D008687', 'Metformin', true, ARRAY['therapeutic use'], 'pubmed/a.xml'), "
            "('200', 'D008687', 'Metformin', true, ARRAY['Adverse Effects'], 'pubmed/b.xml'), "
            "('200', 'D056486', 'Chemical and Drug Induced Liver Injury', false, NULL, "
            " 'pubmed/b.xml'), "
            "('300', 'D001241', 'Aspirin', true, NULL, 'pubmed/c.xml'), "
            "('400', 'D008687', 'Metformin', false, NULL, 'pubmed/d.xml'), "
            "('150', 'D008687', 'Metformin', false, NULL, 'pubmed/e.xml'), "
            "('150', 'D008107', 'Liver Diseases', false, NULL, 'pubmed/e.xml'), "
            "('500', 'D008687', 'Metformin', false, NULL, 'pubmed/f.xml'), "
            "('500', 'D006505', 'Hepatitis', false, NULL, 'pubmed/f.xml'), "
            "('600', 'D008687', 'Metformin', false, NULL, 'pubmed/g.xml'), "
            "('600', 'D007674', 'Kidney Diseases', false, NULL, 'pubmed/g.xml'), "
            "('700', 'D008687', 'Metformin', false, NULL, 'pubmed/h.xml'), "
            "('700', 'D999999', 'Decoy', false, NULL, 'pubmed/h.xml')"
        )
        cur.execute(
            "INSERT INTO episteme.articles (id, source, pmid, year) VALUES "
            "('pubmed:100', 'pubmed', '100', 2020), ('pubmed:200', 'pubmed', '200', 2023), "
            "('pmc:PMC200', 'pmc', '200', 2023), ('pubmed:300', 'pubmed', '300', 2024), "
            "('pubmed:150', 'pubmed', '150', 2019), ('pubmed:500', 'pubmed', '500', 2021), "
            "('pubmed:600', 'pubmed', '600', 2022), ('pubmed:700', 'pubmed', '700', 2022)"
        )
    ref_db.commit()
    yield ref_db
    ref_db.rollback()
    with ref_db.cursor() as cur:
        cur.execute("DELETE FROM episteme.mesh_tree")
        cur.execute("DELETE FROM episteme.reg_release WHERE dataset = 'pubmed'")
    ref_db.commit()


def _ids(rows) -> list[str]:
    return [r[1] for r in rows if r[0] == "article"]


def _notes(rows) -> list[str]:
    return [r[9] for r in rows if r[0] == "note"]


def _call(conn, key, topic, from_year=None, to_year=None, limit=None):
    return _api.call(conn, "literature_by_mesh", key, topic, from_year, to_year, limit)


def _comparable(result):
    """The result with the claims sorted by value: the legacy path orders equal years
    by PMID descending, the SQL function by article_id ascending."""
    return dataclasses.replace(result, claims=tuple(sorted(result.claims, key=lambda c: c.value)))


# Fixed expectations: the results the former Python path (pre-0015) gave on this
# fixture, with the claims sorted by value; the SQL path gave the same (parity run on
# episteme_test before the legacy copy was deleted).
def _pmid(pmid: str, year, mesh: str, letter: str, topic: str | None = None) -> Claim:
    return Claim(
        text=f"PMID {pmid} ({year or 'year unknown'}): MeSH {mesh}"
        + (f"; topic {topic}" if topic else ""),
        value=pmid,
        method="fetched",
        provenance=Provenance("pubmed", None, f"pubmed/{letter}.xml", f"pmid:{pmid}", None),
        confidence=1.0,
        confidence_basis="exact MeSH descriptor name match",
    )


# Every Metformin record, sorted by PMID (400 has no articles row: year unknown).
_METFORMIN = tuple(
    _pmid(p, y, "Metformin", f)
    for p, y, f in (
        ("100", 2020, "a"),
        ("150", 2019, "e"),
        ("200", 2023, "b"),
        ("400", None, "d"),
        ("500", 2021, "f"),
        ("600", 2022, "g"),
        ("700", 2022, "h"),
    )
)


def _found(*claims: Claim) -> LookupResult:
    return LookupResult(claims, Coverage(matched=len(claims)))


@pytest.mark.pg
@pytest.mark.parametrize(
    "key,topic,expected",
    [
        (1, None, _found(*_METFORMIN)),
        (2, None, _found(*_METFORMIN)),  # the salt: the same moiety family
        (3, None, _found(_pmid("300", 2024, "Aspirin", "c"))),
        # a topic with no descendants
        (
            1,
            "Chemical and Drug Induced Liver Injury",
            _found(_pmid("200", 2023, "Metformin", "b", "Chemical and Drug Induced Liver Injury")),
        ),
        # a qualifier name
        (1, "adverse effects", _found(_pmid("200", 2023, "Metformin", "b", "adverse effects"))),
        (1, "Kidney Diseases", _found(_pmid("600", 2022, "Metformin", "g", "Kidney Diseases"))),
        (
            999,
            None,
            LookupResult((), Coverage(0, ("article_mesh",), ("substance 999 not found",))),
        ),
    ],
)
def test_literature_fixed_without_children(db, key, topic, expected):
    new = _comparable(literature(db, key, topic, **KW))
    if topic == "adverse effects":
        # A qualifier name is no descriptor: the SQL function adds a note.
        assert new.claims == expected.claims
        assert new.coverage.matched == expected.coverage.matched
        return
    assert new == expected


@pytest.mark.pg
def test_fixed_with_limit_and_empty_table(db):
    new = literature(db, 1, None, limit=1, **KW)  # one year leader: no tie at the cut
    assert new == LookupResult(
        (_pmid("200", 2023, "Metformin", "b"),),
        Coverage(matched=1, notes=("limited to the 1 newest records",)),
    )
    with db.cursor() as cur:
        cur.execute("TRUNCATE episteme.article_mesh")
    db.commit()
    assert literature(db, 1, None, **KW) == LookupResult(
        (), Coverage(matched=0, missing=("article_mesh",))
    )
    empty = _call(db, 1, None)  # the missing row carries the pubmed licence
    assert [r[0] for r in empty] == ["missing"]
    assert all(r[8] is not None for r in empty)


@pytest.mark.pg
def test_topic_expands_to_descendants(db):
    rows = _call(db, 1, "Liver Diseases")
    # The descriptor itself (150), a child by drug injury (200), a child by hepatitis
    # (500); not the kidney record (600) and not the decoy tree number (700). Newest first.
    assert _ids(rows) == ["pubmed:200", "pubmed:500", "pubmed:150"]
    assert _notes(rows) == []
    # The topic is case-insensitive and trimmed; a leaf topic is only itself.
    assert _ids(_call(db, 1, "  liver DISEASES ")) == _ids(rows)
    assert _ids(_call(db, 1, "Hepatitis")) == ["pubmed:500"]
    # The substance side still applies: aspirin has no liver record.
    assert _ids(_call(db, 3, "Liver Diseases")) == []
    # Through the Python lookup, the claim cites the PMID and the topic.
    res = literature(db, 1, "Liver Diseases", **KW)
    assert [c.value for c in res.claims] == ["200", "500", "150"]
    assert res.claims[0].text.endswith("; topic Liver Diseases")


@pytest.mark.pg
def test_year_filter(db):
    assert _ids(_call(db, 1, None)) == [
        "pubmed:200",
        "pubmed:600",
        "pubmed:700",
        "pubmed:500",
        "pubmed:100",
        "pubmed:150",
        "pubmed:400",  # unknown year last
    ]
    # Both bounds are inclusive; same year ordered by id; unknown years drop out.
    assert _ids(_call(db, 1, None, 2020, 2022)) == [
        "pubmed:600",
        "pubmed:700",
        "pubmed:500",
        "pubmed:100",
    ]
    assert _ids(_call(db, 1, None, 2022, None)) == ["pubmed:200", "pubmed:600", "pubmed:700"]
    assert _ids(_call(db, 1, None, None, 2019)) == ["pubmed:150"]
    assert _ids(_call(db, 1, "Liver Diseases", 2021, 2023)) == ["pubmed:200", "pubmed:500"]
    assert _ids(_call(db, 1, None, 2030, 2031)) == []


@pytest.mark.pg
def test_unknown_topic_note(db):
    rows = _call(db, 1, "No Such Descriptor")
    assert _ids(rows) == []
    assert len(_notes(rows)) == 1 and "No Such Descriptor" in _notes(rows)[0]
    res = literature(db, 1, "No Such Descriptor", **KW)
    assert res.claims == ()
    assert any("No Such Descriptor" in n for n in res.coverage.notes)
    # A qualifier name is not a descriptor: the note is there and the match still works.
    rows = _call(db, 1, "adverse effects")
    assert _ids(rows) == ["pubmed:200"]
    assert len(_notes(rows)) == 1


@pytest.mark.pg
def test_gaps_and_release(db):
    gone = _call(db, 999, None)
    assert [(r[0], r[1], r[2]) for r in gone] == [
        ("note", None, None),
        ("missing", None, "article_mesh"),
    ]
    assert gone[0][9] == "substance 999 not found"
    assert all(r[8] is not None for r in gone if r[0] == "missing")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.reg_release (dataset, release_id, rows, status, is_latest) "
            "VALUES ('pubmed', 'pm-old', 1, 'loaded', false), "
            "('pubmed', 'pm-2025', 1, 'loaded', true)"
        )
    db.commit()
    rows = _call(db, 2, None, limit=1)  # the salt reaches its moiety's records
    assert len(_ids(rows)) == 1
    art = next(r for r in rows if r[0] == "article")
    assert art[1] == "pubmed:200" and art[2] == "pubmed"
    assert art[3] is None and art[4] == 2023 and art[5] == 1.0
    assert art[6] == "Metformin" and art[7] == "pm-2025" and art[8] == "open_metadata"
    assert art[10] == "pubmed/b.xml"
    assert literature(db, 1, None, limit=1, **KW).claims[0].provenance.release == "pm-2025"


@pytest.mark.pg
def test_public_wrapper_caps_articles(db):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM episteme_api.literature_by_mesh(1, NULL, 2020, 2022)")
        rows = cur.fetchall()
    assert _ids(rows) == ["pubmed:600", "pubmed:700", "pubmed:500", "pubmed:100"]
    with db.cursor() as cur:
        cur.execute(
            "UPDATE episteme_api_internal.result_limit SET default_limit = 2 "
            "WHERE family = 'literature'"
        )
        cur.execute("SELECT * FROM episteme_api.literature_by_mesh(1, 'Liver Diseases')")
        rows = cur.fetchall()
    db.rollback()
    assert _ids(rows) == ["pubmed:200", "pubmed:500"]
    assert _notes(rows) == ["truncated: 2 of more than 2"]
    # Seven records match; the wrapper builds limit + 1 = 3 and notes the cut, no total.
    with db.cursor() as cur:
        cur.execute(
            "UPDATE episteme_api_internal.result_limit SET default_limit = 2 "
            "WHERE family = 'literature'"
        )
        cur.execute("SELECT * FROM episteme_api.literature_by_mesh(1, NULL)")
        rows = cur.fetchall()
    db.rollback()
    assert _ids(rows) == ["pubmed:200", "pubmed:600"]
    assert _notes(rows) == ["truncated: 2 of more than 2"]
    # Under the cap: no note.
    with db.cursor() as cur:
        cur.execute("SELECT * FROM episteme_api.literature_by_mesh(3, NULL)")
        rows = cur.fetchall()
    assert _ids(rows) == ["pubmed:300"] and _notes(rows) == []


# ---------------------------------------------------------------------------
# 0016: year windows, the PubMed year only
# ---------------------------------------------------------------------------
@pytest.mark.pg
def test_year_from_pubmed_row_only(db):
    # A manuscript row with a later year must not move the PubMed year.
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.articles (id, source, pmid, year) VALUES "
            "('europepmc_manuscript:M100', 'europepmc_manuscript', '100', 2025)"
        )
    db.commit()
    rows = _call(db, 1, None)
    assert {r[1]: r[4] for r in rows if r[0] == "article"}["pubmed:100"] == 2020


@pytest.mark.pg
@pytest.mark.parametrize("width", [1, 2, 5, 50])
def test_window_width_does_not_change_results(db, width):
    expected = _call(db, 1, None)
    expected_f = _call(db, 1, None, 2020, 2022)
    expected_t = _call(db, 1, "Liver Diseases")
    expected_1 = _call(db, 1, None, limit=1)
    try:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE episteme_api_internal.result_limit SET default_limit = %s, "
                "max_limit = greatest(max_limit, %s) WHERE family = 'literature_window'",
                (width, width),
            )
        db.commit()
        assert _call(db, 1, None) == expected
        assert _call(db, 1, None, 2020, 2022) == expected_f
        assert _call(db, 1, "Liver Diseases") == expected_t
        assert _call(db, 1, None, limit=1) == expected_1
    finally:
        db.rollback()
        api_settings.sync(db)


@pytest.mark.pg
def test_pmid_in_two_windows_appears_once(db):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.articles (id, source, pmid, year) VALUES "
            "('pubmed:600v2', 'pubmed', '600', 2012)"
        )
    db.commit()
    ids = _ids(_call(db, 1, None))
    assert ids.count("pubmed:600") == 1


@pytest.mark.pg
def test_reversed_or_empty_year_range(db):
    assert _ids(_call(db, 1, None, 2024, 2020)) == []
    assert _ids(_call(db, 1, None, 1, 5)) == []


@pytest.mark.pg
def test_windows_cut_ties_and_year_zero(db):
    """Narrow windows: the cut inside a tie (600 and 700, both 2022) is by article_id;
    a PMID whose rows lie in two windows keeps the newer year; a 'pubmed' row with year
    0 is an unknown year (last, and only without a year bound)."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.articles (id, source, pmid, year) VALUES "
            "('pubmed:600v2', 'pubmed', '600', 2012), ('pubmed:400', 'pubmed', '400', 0)"
        )
        cur.execute(
            "UPDATE episteme_api_internal.result_limit SET default_limit = 1, "
            "max_limit = greatest(max_limit, 1) WHERE family = 'literature_window'"
        )
    db.commit()
    try:
        assert _ids(_call(db, 1, None, limit=2)) == ["pubmed:200", "pubmed:600"]
        assert _ids(_call(db, 1, None, limit=3)) == ["pubmed:200", "pubmed:600", "pubmed:700"]
        rows = _call(db, 1, None)
        assert {r[1]: r[4] for r in rows if r[0] == "article"}["pubmed:600"] == 2022
        assert _ids(rows)[-1] == "pubmed:400" and rows[-1][4] is None
        assert "pubmed:400" not in _ids(_call(db, 1, None, None, 2030))
        # An older topic match is found after empty newer windows.
        assert _ids(_call(db, 1, "Liver Diseases", None, 2020)) == ["pubmed:150"]
    finally:
        db.rollback()
        api_settings.sync(db)
