"""current_label, label_section_text and trials as SQL functions (migration 0015), with
the results the former Python query path gave on these fixtures as fixed expectations
(pg: episteme_test only)."""

from __future__ import annotations

import json
import os

import psycopg
import pytest
from psycopg import sql

from episteme.config import api_result_limits
from episteme.reference import api_settings, lookups
from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance

from ._pbrer_db import KW, MIGRATIONS, TEST_AACT_RELEASE, TEST_AACT_SCHEMA, grant_owner

pytestmark = pytest.mark.pg

_MIGRATION = MIGRATIONS / "0015_api_layer.sql"
_AACT_ZIP = f"aact/{TEST_AACT_RELEASE}_export_ctgov.zip"

# A label whose stored title is not empty, so the virtual article_body.text starts
# with the title and differs from body_text. The non-ASCII character before the
# second section checks that the offsets count characters, not bytes.
_TEXT_ID = "dailymed:text1"
_SEC1 = "INDICATIONS AND USAGE\nUse for type 2 diabetes (étude)."
_SEC2 = "ADVERSE REACTIONS\nNausea, diarrhoea."
_BODY = _SEC1 + "\n\n" + _SEC2


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


def _clear_bodies(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM episteme.article_body WHERE source IN ('dailymed', 'ema_epar')")
    conn.commit()


@pytest.fixture
def db(api_schema, ref_db, clean_substance):
    """The ref_db substances (1 metformin, 2 its salt, 3 aspirin) and the API settings
    from config."""
    api_settings.sync(ref_db)
    _clear_bodies(ref_db)
    yield ref_db
    ref_db.rollback()
    _clear_bodies(ref_db)


def _rows(conn, query, params=()):
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def _keys(res) -> list[str]:
    return [c.provenance.record_key for c in res.claims]


def _labels(conn) -> None:
    """The DailyMed fixture of test_lookups: set1 lists the salt UNII (two sections),
    set2 aspirin (one section, no articles row)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.articles (id, source, source_file, year, book_meta) VALUES "
            "('dailymed:set1', 'dailymed', 'dailymed/part-1.parquet', 2024, %s::jsonb)",
            (json.dumps({"doc_type": "34391-3", "version": "7"}),),
        )
        cur.execute(
            "INSERT INTO episteme.label_substance (article_id, unii, ndc_product) VALUES "
            "('dailymed:set1', 'UNIIMET02', '000000001'), ('dailymed:set2', 'UNIIASP03', NULL)"
        )
        cur.execute(
            "INSERT INTO episteme.label_section "
            "(article_id, loinc_code, title, start_offset, end_offset) VALUES "
            "('dailymed:set1', '34084-4', 'ADVERSE REACTIONS', 100, 200), "
            "('dailymed:set1', '34067-9', 'INDICATIONS AND USAGE', 0, 99), "
            "('dailymed:set2', '34084-4', 'ADVERSE REACTIONS', 0, 10)"
        )
    conn.commit()


def _text_label(conn) -> tuple[tuple[int, int], tuple[int, int]]:
    """``_TEXT_ID`` with two sections; returns their (start, end) offsets."""
    s1 = (0, len(_SEC1))
    start2 = _BODY.index(_SEC2)
    s2 = (start2, start2 + len(_SEC2))
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.articles (id, source, source_file, year, book_meta) VALUES "
            "(%s, 'dailymed', 'dailymed/part-9.parquet', 2025, %s::jsonb)",
            (_TEXT_ID, json.dumps({"version": "3"})),
        )
        cur.execute(
            "INSERT INTO episteme.article_body (article_id, source, title, body_text) "
            "VALUES (%s, 'dailymed', 'METFORMIN TABLETS label', %s)",
            (_TEXT_ID, _BODY),
        )
        cur.execute(
            "INSERT INTO episteme.label_section "
            "(article_id, loinc_code, title, start_offset, end_offset) VALUES "
            "(%s, '34067-9', 'INDICATIONS AND USAGE', %s, %s), "
            "(%s, '34084-4', 'ADVERSE REACTIONS', %s, %s)",
            (_TEXT_ID, *s1, _TEXT_ID, *s2),
        )
    conn.commit()
    return s1, s2


# Fixed expectations: the results the former Python path (pre-0015) gave on these
# fixtures; the SQL path gave the same (parity run on episteme_test before the legacy
# copy was deleted).
_GAP = "change history not determinable"


def _section(article: str, loinc: str, title: str, version, year, source_file) -> Claim:
    return Claim(
        text=(
            f"{article} section {loinc} {title}, SPL version {version or 'unknown'}, "
            f"effective year {year or 'unknown'}"
        ),
        value=title,
        method="fetched",
        provenance=Provenance("dailymed", version, source_file, f"{article}#{loinc}", "US"),
        confidence=1.0,
        confidence_basis="exact UNII match in the label's active ingredients",
    )


_P1 = "dailymed/part-1.parquet"
_SET1 = (
    _section("dailymed:set1", "34067-9", "INDICATIONS AND USAGE", "7", 2024, _P1),
    _section("dailymed:set1", "34084-4", "ADVERSE REACTIONS", "7", 2024, _P1),
)
_SET2 = (_section("dailymed:set2", "34084-4", "ADVERSE REACTIONS", None, None, None),)
_NO_LABELS = LookupResult((), Coverage(matched=0, missing=("dailymed",), notes=(_GAP,)))


def _unknown(dataset: str, key: int) -> LookupResult:
    return LookupResult((), Coverage(0, (dataset,), (f"substance {key} not found",)))


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (1, LookupResult(_SET1, Coverage(matched=2, notes=(_GAP,)))),  # set1 lists the salt
        (2, LookupResult(_SET1, Coverage(matched=2, notes=(_GAP,)))),
        (3, LookupResult(_SET2, Coverage(matched=1, notes=(_GAP,)))),  # no articles row
        (999, _unknown("dailymed", 999)),
    ],
)
def test_label_fixed(db, key, expected):
    # Empty label tables first: a missing source.
    empty = _unknown("dailymed", 999) if key == 999 else _NO_LABELS
    assert lookups.current_label(db, key, **KW) == empty
    _labels(db)
    assert lookups.current_label(db, key, **KW) == expected


def test_label_content_and_eu_label(db):
    _labels(db)
    res = lookups.current_label(db, 2, **KW)
    assert _keys(res) == ["dailymed:set1#34067-9", "dailymed:set1#34084-4"]
    assert res.coverage.notes == (lookups.LABEL_HISTORY_GAP,)
    # An EU label linked to the moiety UNII: source, jurisdiction and record key.
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.articles (id, source, source_file, year, book_meta) VALUES "
            "('ema_epar:100', 'ema_epar', 'ema_epar/part-1.parquet', 2023, %s::jsonb)",
            (json.dumps({"last_updated_date": "2025-06-01"}),),
        )
        cur.execute(
            "INSERT INTO episteme.label_substance (article_id, unii) "
            "VALUES ('ema_epar:100', 'UNIIMET01')"
        )
        cur.execute(
            "INSERT INTO episteme.label_section "
            "(article_id, loinc_code, title, start_offset, end_offset, ordinal, product) "
            "VALUES ('ema_epar:100', '34067-9', '4.1 Therapeutic indications', 10, 50, 3, 'X')"
        )
    db.commit()
    res = lookups.current_label(db, 1, **KW)
    eu = [c for c in res.claims if c.provenance.source == "ema_epar"]
    assert len(eu) == 1
    assert eu[0].provenance.record_key == "ema_epar:100#34067-9#3"
    assert eu[0].provenance.jurisdiction == "EU"
    assert eu[0].provenance.release == "2025-06-01"
    assert eu[0].provenance.source_file == "ema_epar/part-1.parquet"


def test_label_section_text_offsets(db):
    s1, s2 = _text_label(db)
    by_code = lookups.label_section_text(db, _TEXT_ID, "34084-4", **KW)
    assert [c.value for c in by_code.claims] == [_BODY[s2[0] : s2[1]]]
    assert by_code.claims[0].value == _SEC2
    by_title = lookups.label_section_text(db, _TEXT_ID, "indications and usage", **KW)
    assert [c.value for c in by_title.claims] == [_SEC1]
    c = by_title.claims[0]
    assert c.provenance.source == "dailymed"
    assert c.provenance.release == "3"
    assert c.provenance.source_file == "dailymed/part-9.parquet"
    assert c.provenance.record_key == f"{_TEXT_ID}#34067-9"
    assert c.provenance.jurisdiction == "US"
    assert by_title.coverage.notes == ()
    assert by_title.coverage.missing == ()
    # The slice is of body_text: the virtual text starts with the title.
    ((text,),) = _rows(
        db, "SELECT text FROM episteme.article_body WHERE article_id = %s", (_TEXT_ID,)
    )
    assert text != _BODY and text.endswith(_BODY)
    # The public function returns the same rows (no cap reached).
    internal = _rows(
        db,
        "SELECT * FROM episteme_api_internal.label_section_text(%s, %s)",
        (_TEXT_ID, "34084-4"),
    )
    public = _rows(
        db, "SELECT * FROM episteme_api.label_section_text(%s, %s)", (_TEXT_ID, "34084-4")
    )
    assert public == internal


def test_label_section_unknown_section_note(db):
    _text_label(db)
    res = lookups.label_section_text(db, _TEXT_ID, "99999-9", **KW)
    assert res.claims == ()
    assert res.coverage.notes == (f"label {_TEXT_ID} has no section 99999-9",)
    unknown = lookups.label_section_text(db, "dailymed:nope", "34084-4", **KW)
    assert unknown.claims == ()
    assert unknown.coverage.notes == ("label dailymed:nope not found",)
    not_a_label = lookups.label_section_text(db, "pmc:PMC1", "34084-4", **KW)
    assert not_a_label.coverage.notes == ("label pmc:PMC1 not found",)
    _labels(db)  # set1 has sections but no article_body row
    no_text = lookups.label_section_text(db, "dailymed:set1", "34084-4", **KW)
    assert no_text.claims == ()
    assert no_text.coverage.notes == ("label dailymed:set1 has no stored text",)


def _aact(conn, *, latest: bool, load_state: bool = True) -> None:
    s = sql.Identifier(TEST_AACT_SCHEMA)
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(s))
        cur.execute(
            sql.SQL(
                "CREATE TABLE {}.studies (nct_id text, brief_title text, overall_status text, "
                "phase text, results_first_posted_date text)"
            ).format(s)
        )
        cur.execute(
            sql.SQL(
                "CREATE TABLE {}.intervention_substance (nct_id text, intervention_id text, "
                "substance_key bigint, moiety_key bigint, method text)"
            ).format(s)
        )
        cur.execute(
            sql.SQL(
                "INSERT INTO {}.studies VALUES "
                "('NCT01', 'Metformin in X', 'COMPLETED', 'PHASE3', '2020-01-02'), "
                "('NCT02', 'Metformin salt in Y', 'RECRUITING', 'PHASE2', NULL), "
                "('NCT03', 'Aspirin in Z', 'COMPLETED', 'PHASE4', NULL), "
                "('NCT04', '', NULL, NULL, NULL)"
            ).format(s)
        )
        cur.execute(
            sql.SQL(
                "INSERT INTO {}.intervention_substance VALUES "
                "('NCT01', 'i1', 1, 1, 'normalised_name'), "
                "('NCT01', 'i2', 1, 1, 'normalised_other_name'), "
                "('NCT02', 'i3', 2, 1, 'normalised_name'), "
                "('NCT03', 'i4', 3, 3, 'normalised_name'), "
                "('NCT04', 'i5', 3, 3, 'normalised_name')"
            ).format(s)
        )
        if load_state:
            cur.execute(
                sql.SQL(
                    "CREATE TABLE {}._load_state (table_name text PRIMARY KEY, "
                    "fingerprint text NOT NULL, source_file text NOT NULL, rows bigint NOT NULL, "
                    "stats jsonb NOT NULL, done_at timestamptz NOT NULL DEFAULT now())"
                ).format(s)
            )
            cur.execute(
                sql.SQL(
                    "INSERT INTO {}._load_state (table_name, fingerprint, source_file, rows, "
                    "stats) VALUES ('_release', 'fp', %s, 4, '{{}}'::jsonb), "
                    "('studies', 'fp', 'other', 4, '{{}}'::jsonb)"
                ).format(s),
                (_AACT_ZIP,),
            )
        cur.execute(
            "INSERT INTO episteme.reg_release (dataset, release_id, rows, status, is_latest) "
            "VALUES ('aact', %s, 4, 'loaded', %s)",
            (TEST_AACT_RELEASE, latest),
        )
    conn.commit()
    grant_owner(conn)


def _trial(nct: str, text: str, status) -> Claim:
    return Claim(
        text=f"{nct} {text}",
        value=status,
        method="fetched",
        provenance=Provenance("aact", TEST_AACT_RELEASE, _AACT_ZIP, nct, None),
        confidence=1.0,
        confidence_basis="intervention name matched to the substance by exact normalised name",
    )


_NCT_METFORMIN = (
    _trial(
        "NCT01",
        "Metformin in X: status COMPLETED, phase PHASE3, results posted: yes",
        "COMPLETED",
    ),
    _trial(
        "NCT02",
        "Metformin salt in Y: status RECRUITING, phase PHASE2, results posted: no",
        "RECRUITING",
    ),
)
_NCT_ASPIRIN = (
    _trial(
        "NCT03", "Aspirin in Z: status COMPLETED, phase PHASE4, results posted: no", "COMPLETED"
    ),
    _trial("NCT04", "(no title): status unknown, phase unknown, results posted: no", None),
)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (1, LookupResult(_NCT_METFORMIN, Coverage(matched=2))),
        (2, LookupResult(_NCT_METFORMIN, Coverage(matched=2))),
        (3, LookupResult(_NCT_ASPIRIN, Coverage(matched=2))),
        (999, _unknown("aact", 999)),
    ],
)
def test_trials_fixed_and_source_file(db, key, expected):
    # No AACT release at all: a missing source.
    empty = _unknown("aact", 999) if key == 999 else LookupResult((), Coverage(0, ("aact",)))
    assert lookups.trials(db, key, **KW) == empty
    _aact(db, latest=True)
    # The former claims, with source_file filled from the release's _load_state row
    # (the former path gave None).
    assert lookups.trials(db, key, **KW) == expected


def test_trials_not_latest_and_no_load_state(db):
    _aact(db, latest=False)
    res = lookups.trials(db, 1, **KW)
    assert res == LookupResult((), Coverage(matched=0, missing=("aact",)))
    db.rollback()
    with db.cursor() as cur:
        cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(TEST_AACT_SCHEMA)))
        cur.execute("DELETE FROM episteme.reg_release WHERE dataset = 'aact'")
    db.commit()
    _aact(db, latest=True, load_state=False)
    res = lookups.trials(db, 3, **KW)
    assert _keys(res) == ["NCT03", "NCT04"]
    assert {c.provenance.source_file for c in res.claims} == {None}
    assert "(no title): status unknown, phase unknown" in res.claims[1].text


def test_public_label_truncates(db):
    limit = api_result_limits()["label"][0]
    assert limit == 200
    total = 250
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.label_substance (article_id, unii) "
            "VALUES ('dailymed:big', 'UNIIASP03')"
        )
        cur.executemany(
            "INSERT INTO episteme.label_section "
            "(article_id, loinc_code, title, start_offset, end_offset) "
            "VALUES ('dailymed:big', %s, %s, %s, %s)",
            [(f"code-{i:03d}", f"Section {i}", i * 10, i * 10 + 9) for i in range(total)],
        )
    db.commit()
    internal = _rows(db, "SELECT * FROM episteme_api_internal.current_label(3)")
    public = _rows(db, "SELECT * FROM episteme_api.current_label(3)")
    internal_claims = [r for r in internal if r[0] == "claim"]
    assert len(internal_claims) == total
    public_claims = [r for r in public if r[0] == "claim"]
    assert public_claims == internal_claims[:limit]
    notes = [r[3] for r in public if r[0] == "note"]
    assert notes == [lookups.LABEL_HISTORY_GAP, f"truncated: {limit} of {total}"]
    assert public[-1][3] == f"truncated: {limit} of {total}"
