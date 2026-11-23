"""approval_status and safety_actions as SQL functions (migration 0015), with the results
the former Python query path gave on these fixtures as fixed expectations (pg:
episteme_test only)."""

from __future__ import annotations

import json
import os
from datetime import date

import psycopg
import pytest

from episteme.config import LOOKUP_SOURCE_SUBSETS
from episteme.reference import api_settings, lookups
from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance

from ._pbrer_db import KW, MIGRATIONS, add_release

pytestmark = pytest.mark.pg

_MIGRATION = MIGRATIONS / "0015_api_layer.sql"
_START, _END = date(2025, 1, 1), date(2025, 3, 31)


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
    """The ref_db substances (1 metformin, 2 its salt, 3 aspirin), substance 7 with no
    name, the API settings from config, and the regulatory fixture releases."""
    api_settings.sync(ref_db)
    with ref_db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.substance (substance_key, unii, preferred_name, moiety_key) "
            "VALUES (7, NULL, NULL, 7)"
        )
    ref_db.commit()
    _approval_releases(ref_db)
    _safety_releases(ref_db)
    return ref_db


def _approval_releases(conn) -> None:
    # A release that is not latest: never read.
    add_release(
        conn,
        "fda_drugsfda",
        "2026-09",
        [{"record_key": "NDA-OLD", "substance_names": ["metformin"]}],
        latest=False,
    )
    add_release(
        conn,
        "fda_drugsfda",
        "2026-10",
        [
            {
                # products[] walked; a repeated value once; blank and null values skipped.
                "record_key": "NDA-1",
                "substance_names": ["metformin hydrochloride"],
                "event_date": date(2001, 1, 1),
                "payload": {
                    "sponsor_name": "  ACME  ",
                    "products": [
                        {"brand_name": "GLUCOPHAGE", "marketing_status": "Prescription"},
                        {"brand_name": "GLUCOPHAGE XR", "marketing_status": "Prescription"},
                        {"brand_name": "   ", "marketing_status": None},
                        "not an object",
                    ],
                },
            },
            {
                # Same date as NDA-1: record_key breaks the tie. No status value: note.
                # An empty jurisdiction is left out of the text.
                "record_key": "NDA-0",
                "substance_names": ["metformin"],
                "event_date": date(2001, 1, 1),
                "jurisdiction": "",
                "payload": {"sponsor_name": "Beta", "products": [{"brand_name": None}]},
            },
            {
                # No date: last (NULLS LAST).
                "record_key": "NDA-2",
                "substance_names": ["dimethylbiguanide"],
                "payload": {"products": {"marketing_status": ["Discontinued", " ", None]}},
            },
            {"record_key": "NDA-ASP", "substance_names": ["aspirin"]},
        ],
        latest=True,
    )
    add_release(
        conn,
        "ema_article57",
        "2026-10",
        [
            {
                "record_key": "a57-1",
                "substance_names": ["metformin"],
                "jurisdiction": "EU",
                "payload": {
                    "product_name": "Glucophage",
                    "product_authorisation_country": ["Germany", "France", "Germany"],
                    "marketing_authorisation_holder": 42,
                },
            }
        ],
        latest=True,
    )
    add_release(
        conn,
        "ema_outside_eu",
        "2026-10",
        [
            {
                "record_key": "EMEA/H/W/1",
                "substance_names": ["metformin"],
                "jurisdiction": "EU",
                "event_date": date(2020, 6, 1),
                "payload": {"ema_opinion_status": "Positive", "name_of_medicine": "Metfo"},
            }
        ],
        latest=True,
    )
    # ema_medicines has no release: missing.


def _safety_releases(conn) -> None:
    add_release(
        conn,
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
                "record_key": "D-start",
                "substance_names": ["metformin hydrochloride"],
                "event_date": _START,
                "payload": {"status": "Ongoing", "reason_for_recall": "NDMA"},
            },
            {"record_key": "D-end", "substance_names": ["metformin"], "event_date": _END},
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
            {"record_key": "D-nodate", "substance_names": ["metformin"]},
            {"record_key": "D-nodate2", "substance_names": ["metformin hydrochloride"]},
            {
                "record_key": "D-other",
                "substance_names": ["aspirin"],
                "event_date": date(2025, 2, 1),
            },
        ],
        latest=True,
    )
    add_release(
        conn,
        "ema_dhpc",
        "2026-10",
        [
            {
                "record_key": "DHPC-1",
                "substance_names": ["metformin"],
                "jurisdiction": "EU",
                "event_date": date(2025, 3, 1),
                "payload": {
                    "dhpc_type": "Safety signal",
                    "regulatory_outcome": "Variation",
                    "name_of_medicine": "Glucophage",
                },
            }
        ],
        latest=True,
    )
    add_release(
        conn,
        "ema_psusa",
        "2026-10",
        [
            {
                "record_key": "PSUSA-1",
                "substance_names": ["metformin"],
                "jurisdiction": "EU",
                "event_date": date(2025, 1, 15),
                "payload": {"procedure_number": "PSUSA/00001/202501"},
            }
        ],
        latest=True,
    )
    # ema_referrals has no release: missing.


def _rows(conn, query, params=()):
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def _keys(res) -> list[str]:
    return [c.provenance.record_key for c in res.claims]


# Fixed expectations: the results the former Python path (pre-0015) gave on this
# fixture; the SQL path gave the same (parity run on episteme_test before the legacy
# copy was deleted).
def _reg(ds: str, key: str, text: str, value: str | None, jurisdiction: str = "US") -> Claim:
    return Claim(
        text=text,
        value=value,
        method="fetched",
        provenance=Provenance(ds, "2026-10", f"{ds}/2026-10.json", key, jurisdiction),
        confidence=1.0,
        confidence_basis="exact normalised substance name in the latest release",
    )


_NDA0 = _reg(
    "fda_drugsfda", "NDA-0", "fda_drugsfda NDA-0, sponsor_name: Beta, date: 2001-01-01", None, ""
)
_NDA1 = _reg(
    "fda_drugsfda",
    "NDA-1",
    "fda_drugsfda NDA-1, sponsor_name: ACME, brand_name: GLUCOPHAGE; GLUCOPHAGE XR, "
    "status: Prescription, date: 2001-01-01, jurisdiction: US",
    "Prescription",
)
_NDA2 = _reg(
    "fda_drugsfda",
    "NDA-2",
    "fda_drugsfda NDA-2, status: Discontinued; None, jurisdiction: US",
    "Discontinued; None",
)
_A57 = _reg(
    "ema_article57",
    "a57-1",
    "ema_article57 a57-1, product_name: Glucophage, product_authorisation_country: "
    "Germany; France, marketing_authorisation_holder: 42, jurisdiction: EU",
    None,
    "EU",
)
_OUTSIDE = _reg(
    "ema_outside_eu",
    "EMEA/H/W/1",
    "ema_outside_eu EMEA/H/W/1, name_of_medicine: Metfo, status: Positive, date: 2020-06-01, "
    "jurisdiction: EU",
    "Positive",
    "EU",
)
_NDA_ASP = _reg("fda_drugsfda", "NDA-ASP", "fda_drugsfda NDA-ASP, jurisdiction: US", None)
_D_END = _reg(
    "fda_enforcement", "D-end", "fda_enforcement D-end, date: 2025-03-31, jurisdiction: US", None
)
_D_IN = _reg(
    "fda_enforcement",
    "D-in",
    "fda_enforcement D-in, recall_number: D-in, status: Class II, date: 2025-02-01, "
    "jurisdiction: US",
    "Class II",
)
_D_START = _reg(
    "fda_enforcement",
    "D-start",
    "fda_enforcement D-start, reason_for_recall: NDMA, status: Ongoing, date: 2025-01-01, "
    "jurisdiction: US",
    "Ongoing",
)
_D_OTHER = _reg(
    "fda_enforcement",
    "D-other",
    "fda_enforcement D-other, date: 2025-02-01, jurisdiction: US",
    None,
)
_DHPC = _reg(
    "ema_dhpc",
    "DHPC-1",
    "ema_dhpc DHPC-1, name_of_medicine: Glucophage, dhpc_type: Safety signal, "
    "status: Safety signal, date: 2025-03-01, jurisdiction: EU",
    "Safety signal",
    "EU",
)
_PSUSA = _reg(
    "ema_psusa",
    "PSUSA-1",
    "ema_psusa PSUSA-1, procedure_number: PSUSA/00001/202501, date: 2025-01-15, jurisdiction: EU",
    None,
    "EU",
)
_FDA_NOTE = "fda_drugsfda: status field not found"
_UNDATED = "fda_enforcement: 2 records with no date not checked"
_ENF_NOTE = "fda_enforcement: status field not found"
_PSUSA_NOTE = "ema_psusa: status field not found"
_APPROVAL_METFORMIN = LookupResult(
    (_NDA0, _NDA1, _NDA2, _A57, _OUTSIDE),
    Coverage(matched=5, missing=("ema_medicines",), notes=(_FDA_NOTE,)),
)
_SAFETY_METFORMIN = LookupResult(
    (_D_END, _D_IN, _D_START, _DHPC, _PSUSA),
    Coverage(
        matched=5,
        missing=("ema_referrals",),
        notes=(_UNDATED, _ENF_NOTE, _PSUSA_NOTE),
    ),
)


def _unknown(datasets: tuple[str, ...], key: int) -> LookupResult:
    return LookupResult((), Coverage(0, datasets, (f"substance {key} not found",)))


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (1, _APPROVAL_METFORMIN),
        (2, _APPROVAL_METFORMIN),  # the salt: the same moiety family
        (
            3,
            LookupResult(
                (_NDA_ASP,), Coverage(matched=1, missing=("ema_medicines",), notes=(_FDA_NOTE,))
            ),
        ),
        (
            7,
            LookupResult(
                (),
                Coverage(
                    matched=0,
                    missing=("ema_medicines",),
                    notes=("substance 7 has no name to match",),
                ),
            ),
        ),
        (999, _unknown(("fda_drugsfda", "ema_medicines", "ema_article57", "ema_outside_eu"), 999)),
    ],
)
def test_approval_fixed(db, key, expected):
    assert lookups.approval_status(db, key, **KW) == expected


def test_approval_content(db):
    res = lookups.approval_status(db, 2, **KW)
    assert _keys(res) == ["NDA-0", "NDA-1", "NDA-2", "a57-1", "EMEA/H/W/1"]
    nda0, nda1, nda2, a57, outside = res.claims
    assert nda1.value == "Prescription"
    assert nda1.text == (
        "fda_drugsfda NDA-1, sponsor_name: ACME, brand_name: GLUCOPHAGE; GLUCOPHAGE XR, "
        "status: Prescription, date: 2001-01-01, jurisdiction: US"
    )
    assert nda0.value is None
    assert nda0.provenance.jurisdiction == ""
    assert "jurisdiction" not in nda0.text
    # A JSON null inside a final list is 'None', as Python str() wrote it (parity).
    assert nda2.value == "Discontinued; None"
    assert a57.value is None
    assert outside.value == "Positive"
    assert res.coverage.missing == ("ema_medicines",)
    # One status note for fda_drugsfda; none for Article 57 (no status field by design).
    assert res.coverage.notes == ("fda_drugsfda: status field not found",)
    assert res.coverage.matched == 5


def test_no_name_and_unknown_substance(db):
    res = lookups.approval_status(db, 7, **KW)
    assert res.claims == ()
    assert res.coverage.notes == ("substance 7 has no name to match",)
    assert res.coverage.missing == ("ema_medicines",)
    unknown = lookups.safety_actions(db, 999, _START, _END, **KW)
    assert unknown.claims == ()
    assert unknown.coverage.missing == lookups.PBRER_DATASETS["safety_actions"]
    assert unknown.coverage.notes == ("substance 999 not found",)


@pytest.mark.parametrize(
    ("key", "start", "end", "expected"),
    [
        (1, _START, _END, _SAFETY_METFORMIN),
        (2, _START, _END, _SAFETY_METFORMIN),
        # one-day interval on the upper edge
        (
            1,
            _END,
            _END,
            LookupResult(
                (_D_END,),
                Coverage(matched=1, missing=("ema_referrals",), notes=(_UNDATED, _ENF_NOTE)),
            ),
        ),
        # empty interval: the undated records are still counted
        (
            1,
            _END,
            _START,
            LookupResult((), Coverage(matched=0, missing=("ema_referrals",), notes=(_UNDATED,))),
        ),
        (
            3,
            _START,
            _END,
            LookupResult(
                (_D_OTHER,),
                Coverage(matched=1, missing=("ema_referrals",), notes=(_ENF_NOTE,)),
            ),
        ),
        (
            999,
            _START,
            _END,
            _unknown(("fda_enforcement", "ema_referrals", "ema_dhpc", "ema_psusa"), 999),
        ),
    ],
)
def test_safety_fixed_interval(db, key, start, end, expected):
    assert lookups.safety_actions(db, key, start, end, **KW) == expected


def test_safety_interval_edges(db):
    res = lookups.safety_actions(db, 1, _START, _END, **KW)
    # Both edges are inside [start, end]; event_date DESC across one dataset.
    assert _keys(res) == ["D-end", "D-in", "D-start", "DHPC-1", "PSUSA-1"]
    assert res.claims[1].value == "Class II"
    assert res.claims[2].value == "Ongoing"
    assert res.claims[3].value == "Safety signal"
    assert res.coverage.notes == (
        "fda_enforcement: 2 records with no date not checked",
        "fda_enforcement: status field not found",
        "ema_psusa: status field not found",
    )
    assert res.coverage.missing == ("ema_referrals",)


def test_missing_dataset_row(db):
    with db.cursor() as cur:
        cur.execute(
            "UPDATE episteme.reg_release SET is_latest = false WHERE dataset = 'ema_article57'"
        )
        cur.execute("UPDATE episteme.reg_release SET is_latest = false WHERE dataset = 'ema_dhpc'")
    db.commit()
    approval = lookups.approval_status(db, 1, **KW)
    assert approval == LookupResult(
        (_NDA0, _NDA1, _NDA2, _OUTSIDE),
        Coverage(matched=4, missing=("ema_medicines", "ema_article57"), notes=(_FDA_NOTE,)),
    )
    assert approval.coverage.missing == ("ema_medicines", "ema_article57")
    assert "a57-1" not in _keys(approval)
    assert {c.provenance.source for c in approval.claims} == {"fda_drugsfda", "ema_outside_eu"}
    safety = lookups.safety_actions(db, 1, _START, _END, **KW)
    assert safety == LookupResult(
        (_D_END, _D_IN, _D_START, _PSUSA),
        Coverage(
            matched=4,
            missing=("ema_referrals", "ema_dhpc"),
            notes=(_UNDATED, _ENF_NOTE, _PSUSA_NOTE),
        ),
    )
    assert safety.coverage.missing == ("ema_referrals", "ema_dhpc")
    assert {c.provenance.source for c in safety.claims} == {"fda_enforcement", "ema_psusa"}
    rows = _rows(
        db,
        "SELECT kind, source, licence FROM episteme_api_internal.approval_status(1) "
        "WHERE kind = 'missing'",
    )
    assert rows == [
        ("missing", "ema_medicines", LOOKUP_SOURCE_SUBSETS["ema_medicines"]),
        ("missing", "ema_article57", LOOKUP_SOURCE_SUBSETS["ema_article57"]),
    ]


def test_pending_rows_returned_with_tag(db):
    before = _rows(db, "SELECT * FROM episteme_api_internal.approval_status(1)")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE episteme_api_internal.source_subset SET subset = 'pending' "
            "WHERE source = 'fda_drugsfda'"
        )
    db.commit()
    try:
        for schema in ("episteme_api_internal", "episteme_api"):
            rows = _rows(db, f"SELECT kind, source, licence FROM {schema}.approval_status(1)")
            assert len(rows) == len(before)
            fda = [r for r in rows if r[0] == "claim" and r[1] == "fda_drugsfda"]
            assert len(fda) == 3
            assert {r[2] for r in fda} == {"pending"}
            others = {r[2] for r in rows if r[0] == "claim" and r[1] != "fda_drugsfda"}
            assert others == {"commercial"}
    finally:
        api_settings.sync(db)


def test_claim_rows_numbered(db):
    rows = _rows(
        db,
        "SELECT kind, claim_group, alt_rank, confidence FROM "
        "episteme_api_internal.safety_actions(1, %s, %s)",
        (_START, _END),
    )
    claims = [r for r in rows if r[0] == "claim"]
    assert [r[1] for r in claims] == [1, 2, 3, 4, 5]
    assert {r[2] for r in claims} == {0}
    assert {r[3] for r in claims} == {1.0}
    assert {(r[1], r[2]) for r in rows if r[0] != "claim"} == {(None, None)}


def test_public_regulatory_caps(db):
    with db.cursor() as cur:
        cur.execute(
            "UPDATE episteme_api_internal.result_limit SET default_limit = 2 "
            "WHERE family = 'regulatory'"
        )
    db.commit()
    try:
        internal = _rows(db, "SELECT * FROM episteme_api_internal.approval_status(1)")
        public = _rows(db, "SELECT * FROM episteme_api.approval_status(1)")
        assert [r for r in public if r[0] == "claim"] == [r for r in internal if r[0] == "claim"][
            :2
        ]
        assert public[-1][0] == "note"
        assert public[-1][3] == "truncated: 2 of 5"
        safety = _rows(
            db, "SELECT kind, text FROM episteme_api.safety_actions(1, %s, %s)", (_START, _END)
        )
        assert safety[-1] == ("note", "truncated: 2 of 5")
    finally:
        api_settings.sync(db)


# (status fields, detail fields, "status field not found" note) per dataset, as the
# former Python path defined them (_STATUS_FIELDS, _DETAIL_FIELDS, _NO_STATUS_FIELD;
# printed from the legacy copy before it was deleted).
_REG_FIELDS = {
    "fda_drugsfda": (
        ("products.marketing_status",),
        ("sponsor_name", "products.brand_name"),
        True,
    ),
    "ema_medicines": (
        ("medicine_status",),
        ("name_of_medicine", "marketing_authorisation_developer_applicant_holder"),
        True,
    ),
    "ema_article57": (
        (),
        ("product_name", "product_authorisation_country", "marketing_authorisation_holder"),
        False,
    ),
    "ema_outside_eu": (("ema_opinion_status",), ("name_of_medicine",), True),
    "fda_enforcement": (
        ("classification", "status"),
        ("recall_number", "reason_for_recall"),
        True,
    ),
    "ema_referrals": (("current_status",), ("referral_name", "reference_number"), True),
    "ema_dhpc": (("dhpc_type", "regulatory_outcome"), ("name_of_medicine", "dhpc_type"), True),
    "ema_psusa": (("regulatory_outcome",), ("procedure_number",), True),
}


def test_reg_fields_match_former_python(db):
    datasets = lookups.APPROVAL_DATASETS + lookups.SAFETY_DATASETS
    assert set(datasets) == set(_REG_FIELDS)
    for ds in datasets:
        ((status, detail, note),) = _rows(
            db, "SELECT * FROM episteme_api_internal.reg_fields(%s)", (ds,)
        )
        assert (tuple(status), tuple(detail), note) == _REG_FIELDS[ds], ds


# The expected values are the output of the former Python ``_get`` on each payload
# (printed from the legacy copy before it was deleted).
@pytest.mark.parametrize(
    ("payload", "path", "expected"),
    [
        ({"a": " x "}, "a", ["x"]),
        ({"a": None}, "a", []),
        ({"a": ["x", "x", " ", "y", None]}, "a", ["x", "y", "None"]),
        ({"a": [{"b": "1"}, {"b": None}, "s", {"b": ["2", "1"]}]}, "a.b", ["1", "2"]),
        ({"a": {"b": {"c": "deep"}}}, "a.b.c", ["deep"]),
        ([{"a": "top"}, {"a": "list"}], "a", ["top", "list"]),
        ({"a": 42}, "a", ["42"]),
        ({"a": [[{"b": "nested"}]]}, "a.b", []),
        ({}, "a.b", []),
        ({"a": ""}, "a", []),
    ],
)
def test_json_values_matches_former_get(db, payload, path, expected):
    ((values,),) = _rows(
        db,
        "SELECT episteme_api_internal.json_values(%s::jsonb, %s)",
        (json.dumps(payload), path),
    )
    assert values == expected
