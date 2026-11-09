"""PBRER v1 lookups against fixture rows (pg)."""

from __future__ import annotations

import json
from datetime import date

import pytest
from psycopg import sql

from episteme.reference.lookups import (
    FAERS_LABEL,
    LABEL_HISTORY_GAP,
    PBRER_DATASETS,
    approval_status,
    current_label,
    faers_counts,
    safety_actions,
    substance_scope,
    trials,
)

from ._pbrer_db import KW, TEST_AACT_SCHEMA, add_release

pytestmark = pytest.mark.pg


def _keys(res) -> list[str]:
    return [c.provenance.record_key for c in res.claims]


def test_approval_status_reads_only_latest_release(ref_db):
    add_release(
        ref_db,
        "fda_drugsfda",
        "2026-09",
        [{"record_key": "NDA-OLD", "substance_names": ["metformin"]}],
        latest=False,
    )
    add_release(
        ref_db,
        "fda_drugsfda",
        "2026-10",
        [
            {
                "record_key": "NDA020357",
                "substance_names": ["metformin hydrochloride"],
                "payload": {
                    "sponsor_name": "ACME",
                    "products": [{"brand_name": "GLUCOPHAGE", "marketing_status": "Prescription"}],
                },
            },
            {"record_key": "NDA-ASP", "substance_names": ["aspirin"]},
        ],
        latest=True,
    )
    # A dataset whose only release is not latest is a gap, not a fallback.
    add_release(
        ref_db,
        "ema_article57",
        "2026-09",
        [{"record_key": "a57", "substance_names": ["metformin"], "jurisdiction": "EU"}],
        latest=False,
    )
    res = approval_status(ref_db, 2, **KW)  # the salt: its own names and the moiety's
    assert _keys(res) == ["NDA020357"]
    (c,) = res.claims
    assert c.method == "fetched"
    assert c.value == "Prescription"
    assert "GLUCOPHAGE" in c.text and "ACME" in c.text
    assert c.provenance.source == "fda_drugsfda"
    assert c.provenance.release == "2026-10"
    assert c.provenance.source_file == "fda_drugsfda/2026-10.json"
    assert c.provenance.jurisdiction == "US"
    assert res.coverage.matched == 1
    assert set(res.coverage.missing) == {"ema_medicines", "ema_article57", "ema_outside_eu"}


def test_missing_dataset_is_a_coverage_gap(ref_db):
    add_release(
        ref_db,
        "ema_medicines",
        "2026-10",
        [
            {
                "record_key": "EMEA/H/C/1",
                "substance_names": ["metformin"],
                "jurisdiction": "EU",
                "event_date": date(2001, 5, 2),
            }
        ],
        latest=True,
    )
    # The salt (key 2) reaches the moiety's names.
    res = approval_status(ref_db, 2, **KW)
    assert "fda_drugsfda" in res.coverage.missing
    assert "ema_medicines" not in res.coverage.missing
    assert _keys(res) == ["EMEA/H/C/1"]
    assert res.claims[0].provenance.jurisdiction == "EU"


def test_unknown_substance_is_a_gap(ref_db):
    res = approval_status(ref_db, 999, **KW)
    assert res.claims == ()
    assert res.coverage.missing == PBRER_DATASETS["approval_status"]


def test_safety_actions_filter_by_interval(ref_db):
    add_release(
        ref_db,
        "fda_enforcement",
        "2026-10",
        [
            {
                "record_key": "D-in",
                "substance_names": ["metformin"],
                "event_date": date(2025, 2, 1),
                "payload": {"classification": "Class II", "recall_number": "D-in"},
            },
            {
                "record_key": "D-before",
                "substance_names": ["metformin"],
                "event_date": date(2024, 12, 31),
            },
            {
                "record_key": "D-after",
                "substance_names": ["metformin"],
                "event_date": date(2025, 4, 1),
            },
            {
                "record_key": "D-edge",
                "substance_names": ["metformin"],
                "event_date": date(2025, 3, 31),
            },
            {"record_key": "D-nodate", "substance_names": ["metformin"]},
            {
                "record_key": "D-other",
                "substance_names": ["aspirin"],
                "event_date": date(2025, 2, 1),
            },
        ],
        latest=True,
    )
    res = safety_actions(ref_db, 1, date(2025, 1, 1), date(2025, 3, 31), **KW)
    assert _keys(res) == ["D-edge", "D-in"]
    assert res.claims[1].value == "Class II"
    assert "fda_enforcement: 1 records with no date not checked" in res.coverage.notes
    assert set(res.coverage.missing) == {"ema_referrals", "ema_dhpc", "ema_psusa"}


def _label(conn) -> None:
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


def test_current_label_reports_history_gap(ref_db):
    _label(ref_db)
    res = current_label(ref_db, 2, **KW)
    assert res.coverage.notes == (LABEL_HISTORY_GAP,)
    assert res.coverage.notes == ("change history not determinable",)
    assert _keys(res) == ["dailymed:set1#34067-9", "dailymed:set1#34084-4"]
    c = res.claims[1]
    assert c.value == "ADVERSE REACTIONS"
    assert c.provenance.release == "7"
    assert c.provenance.source == "dailymed"
    assert c.provenance.source_file == "dailymed/part-1.parquet"
    assert "2024" in c.text
    # A substance with no label still reports the gap.
    with ref_db.cursor() as cur:
        cur.execute("DELETE FROM episteme.label_substance")
    empty = current_label(ref_db, 2, **KW)
    assert empty.claims == ()
    assert empty.coverage.notes == (LABEL_HISTORY_GAP,)


def test_faers_claims_are_computed_and_labelled(ref_db):
    with ref_db.cursor() as cur:
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
    ref_db.commit()
    # Key 2 is a salt: FAERS counts its moiety (key 1). Feb..Jun overlaps Q1 and Q2.
    res = faers_counts(ref_db, 2, date(2025, 2, 15), date(2025, 6, 30), **KW)
    assert [c.value for c in res.claims] == ["5", "6"]
    for c in res.claims:
        assert c.method == "computed"
        assert c.text.endswith("(US public spontaneous reports (FAERS))")
        assert c.text.endswith(f"({FAERS_LABEL})")
        assert c.provenance.jurisdiction == "US"
    assert "2025Q1" in res.claims[0].text and "1000" in res.claims[0].text
    assert res.claims[0].provenance.record_key == "faers_moiety_quarter:1:2025-01-01"
    assert res.coverage.matched == 2


def test_empty_aact_schema_is_a_gap_not_an_error(ref_db):
    with ref_db.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(TEST_AACT_SCHEMA)))
        cur.execute(
            "INSERT INTO episteme.reg_release (dataset, release_id, rows, status, is_latest) "
            "VALUES ('aact', '20261103', 0, 'loaded', true)"
        )
    ref_db.commit()
    res = trials(ref_db, 1, **KW)
    assert res.claims == ()
    assert res.coverage.missing == ("aact",)


def test_aact_without_latest_release_is_a_gap(ref_db):
    _aact(ref_db, latest=False)
    res = trials(ref_db, 1, **KW)
    assert res.claims == ()
    assert res.coverage.missing == ("aact",)


def _aact(conn, *, latest: bool) -> None:
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
                "('NCT03', 'Aspirin in Z', 'COMPLETED', 'PHASE4', NULL)"
            ).format(s)
        )
        cur.execute(
            sql.SQL(
                "INSERT INTO {}.intervention_substance VALUES "
                "('NCT01', 'i1', 1, 1, 'normalised_name'), "
                "('NCT01', 'i2', 1, 1, 'normalised_other_name'), "
                "('NCT02', 'i3', 2, 1, 'normalised_name'), "
                "('NCT03', 'i4', 3, 3, 'normalised_name')"
            ).format(s)
        )
        cur.execute(
            "INSERT INTO episteme.reg_release (dataset, release_id, rows, status, is_latest) "
            "VALUES ('aact', '20261103', 3, 'loaded', %s)",
            (latest,),
        )
    conn.commit()


def test_trials_report_status_phase_and_results(ref_db):
    _aact(ref_db, latest=True)
    res = trials(ref_db, 2, **KW)  # the salt and its moiety
    assert _keys(res) == ["NCT01", "NCT02"]
    first, second = res.claims
    assert first.value == "COMPLETED"
    assert "PHASE3" in first.text and "results posted: yes" in first.text
    assert "results posted: no" in second.text
    assert first.provenance.release == "20261103"
    assert res.coverage.missing == ()


def test_moiety_input_finds_rows_named_by_the_salt(ref_db):
    """The moiety key (1) reaches its salt (2): an approval named only by the salt, a
    label that lists only the salt UNII, a trial matched only to the salt."""
    scope = substance_scope(ref_db, 1)
    assert scope.keys == (1, 2)
    assert scope.moiety_key == 1
    assert set(scope.uniis) == {"UNIIMET01", "UNIIMET02"}
    assert "metformin hydrochloride" in scope.names
    assert "aspirin" not in scope.names
    add_release(
        ref_db,
        "fda_drugsfda",
        "2026-10",
        [
            {"record_key": "NDA-SALT", "substance_names": ["metformin hydrochloride"]},
            {"record_key": "NDA-ASP", "substance_names": ["aspirin"]},
        ],
        latest=True,
    )
    assert _keys(approval_status(ref_db, 1, **KW)) == ["NDA-SALT"]
    _label(ref_db)  # set1 lists only UNIIMET02, the salt
    assert _keys(current_label(ref_db, 1, **KW)) == [
        "dailymed:set1#34067-9",
        "dailymed:set1#34084-4",
    ]
    _aact(ref_db, latest=True)  # NCT02 is matched only to the salt
    assert _keys(trials(ref_db, 1, **KW)) == ["NCT01", "NCT02"]


def test_lookups_write_nothing(ref_db):
    _label(ref_db)
    with ref_db.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
    approval_status(ref_db, 1, **KW)
    safety_actions(ref_db, 1, date(2025, 1, 1), date(2025, 12, 31), **KW)
    current_label(ref_db, 2, **KW)
    trials(ref_db, 1, **KW)
    faers_counts(ref_db, 1, date(2025, 1, 1), date(2025, 12, 31), **KW)


def test_missing_status_field_is_noted_once_per_dataset(ref_db):
    add_release(
        ref_db,
        "fda_drugsfda",
        "2026-10",
        [
            {
                "record_key": "NDA-1",
                "substance_names": ["metformin"],
                "payload": {"products": [{"marketing_status": "Prescription"}]},
            },
            {"record_key": "NDA-2", "substance_names": ["metformin"], "payload": {"x": 1}},
            {"record_key": "NDA-3", "substance_names": ["metformin hydrochloride"]},
        ],
        latest=True,
    )
    add_release(
        ref_db,
        "ema_medicines",
        "2026-10",
        [
            {
                "record_key": "EMEA/H/C/1",
                "substance_names": ["metformin"],
                "jurisdiction": "EU",
                "payload": {"medicine_status": "Authorised"},
            }
        ],
        latest=True,
    )
    res = approval_status(ref_db, 1, **KW)
    values = {c.provenance.record_key: c.value for c in res.claims}
    assert values == {
        "NDA-1": "Prescription",
        "NDA-2": None,
        "NDA-3": None,
        "EMEA/H/C/1": "Authorised",
    }
    status_notes = [n for n in res.coverage.notes if n.endswith("status field not found")]
    assert status_notes == ["fda_drugsfda: status field not found"]


def test_empty_label_tables_are_a_gap(ref_db):
    res = current_label(ref_db, 1, **KW)
    assert res.claims == ()
    assert res.coverage.missing == ("dailymed",)
    assert res.coverage.notes == (LABEL_HISTORY_GAP,)


def test_empty_faers_tables_are_a_gap(ref_db):
    res = faers_counts(ref_db, 1, date(2025, 1, 1), date(2025, 12, 31), **KW)
    assert res.claims == ()
    assert res.coverage.missing == ("faers",)


def test_faers_quarters_without_totals_are_noted(ref_db):
    with ref_db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.faers_moiety_quarter "
            "(substance_key, quarter, reports_suspect, reports_any) VALUES (1, '2025-01-01', 5, 8)"
        )
        cur.execute(
            "INSERT INTO episteme.faers_quarter_totals (quarter, reports) "
            "VALUES ('2025-01-01', 1000)"
        )
    ref_db.commit()
    res = faers_counts(ref_db, 1, date(2025, 2, 1), date(2025, 9, 30), **KW)
    assert [c.value for c in res.claims] == ["5"]
    assert res.coverage.missing == ()
    assert "no FAERS data for quarters: 2025Q2, 2025Q3" in res.coverage.notes
