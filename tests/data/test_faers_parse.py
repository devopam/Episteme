"""FAERS parser: era rules and streaming. Pure: no database.

The fixtures are real reports, trimmed, from the 2004q2, 2012q2 and 2026q2 samples.
"""

import json
import zipfile
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from episteme.data.regulatory.faers import parse

FIXTURES = Path(__file__).parent / "fixtures" / "faers"


def _reports(era: str) -> list[dict]:
    return json.loads((FIXTURES / f"{era}.json").read_text(encoding="utf-8"))


def _by_id(era: str, report_id: str) -> dict:
    return next(r for r in _reports(era) if r["safetyreportid"] == report_id)


def _zip(path: Path, reports: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "drug-event-0001-of-0001.json",
            json.dumps({"meta": {"results": {"total": len(reports)}}, "results": reports}),
        )
    return path


# ---------------------------------------------------------------------------
# Seriousness
# ---------------------------------------------------------------------------
def test_old_era_missing_serious_flag_means_no():
    row, _, _ = parse.report_rows(_by_id("2004q2", "7547238-2"))
    assert row["serious"] is True
    assert row["serious_hospitalization"] is True
    assert row["serious_other"] is True
    # These flags are absent from the old report: absent means "no".
    assert row["serious_death"] is False
    assert row["serious_life_threatening"] is False
    assert row["serious_disabling"] is False
    assert row["serious_congenital"] is False


def test_new_era_flags_are_read_as_given():
    row, _, _ = parse.report_rows(_by_id("2026q2", "26905497"))
    assert row["serious"] is True
    assert row["serious_other"] is True  # '1'
    assert row["serious_death"] is False  # '2'
    assert row["serious_congenital"] is False  # the source spells it ...anomali


def test_non_serious_marker_alone_makes_missing_flags_no():
    row, _, _ = parse.report_rows(_by_id("2004q2", "4435605-5"))
    assert row["serious"] is False
    assert row["serious_death"] is False


def test_report_with_no_seriousness_field_at_all_stays_unknown():
    row, _, _ = parse.report_rows({"safetyreportid": "1", "receivedate": "20200101"})
    assert row["serious"] is None
    assert row["serious_death"] is None
    assert row["serious_other"] is None


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [None, "", "2011", "20111301", "2011061", "not a date"])
def test_report_without_date_goes_to_default_year(bad):
    report = {"safetyreportid": "1", "patient": {"drug": [{"medicinalproduct": "X"}]}}
    if bad is not None:
        report["receivedate"] = bad
    row, drugs, _ = parse.report_rows(report)
    assert row["receive_year"] is None and row["receive_date"] is None
    assert drugs[0]["receive_year"] is None  # NULL routes to the default partition
    counts = Counter()
    parse.count_report(counts, row)
    assert counts["no_date"] == 1


def test_a_report_with_a_date_is_not_counted_as_no_date():
    row, _, _ = parse.report_rows(_by_id("2012q2", "8486749-2"))
    counts = Counter()
    parse.count_report(counts, row)
    assert counts["no_date"] == 0


def test_receive_year_comes_from_receivedate_not_receiptdate():
    row, drugs, reactions = parse.report_rows(_by_id("2004q2", "7547238-2"))
    assert row["receive_date"] == date(2011, 6, 14)
    assert row["receipt_date"] == date(2004, 6, 16)
    assert row["receive_year"] == 2011
    assert {d["receive_year"] for d in drugs} == {2011}
    assert {r["receive_year"] for r in reactions} == {2011}


# ---------------------------------------------------------------------------
# Drugs
# ---------------------------------------------------------------------------
def test_missing_active_substance_falls_back_to_annotation_then_product_name():
    _, drugs, _ = parse.report_rows(_by_id("2004q2", "4435605-5"))
    # No activesubstance, annotation present: from the annotation.
    assert drugs[0]["active_substance"] == "TERIPARATIDE"
    # No activesubstance, no annotation: the normalised product name.
    assert drugs[1]["active_substance"] == "coumadin"
    assert drugs[1]["annotation"] is None
    # The product name stays as published.
    assert drugs[1]["product_name"] == "COUMADIN"


def test_published_active_substance_wins_over_the_annotation():
    _, drugs, _ = parse.report_rows(_by_id("2026q2", "26882264"))
    assert drugs[2]["product_name"] == "VITAMIN C"
    assert drugs[2]["active_substance"] == "ASCORBIC ACID"


def test_combination_fallback_joins_the_names_in_sorted_order():
    _, drugs, _ = parse.report_rows(
        {
            "safetyreportid": "1",
            "patient": {
                "drug": [
                    {
                        "medicinalproduct": "DIOVAN HCT",
                        "openfda": {"substance_name": ["VALSARTAN", "HYDROCHLOROTHIAZIDE"]},
                    }
                ]
            },
        }
    )
    assert drugs[0]["active_substance"] == "HYDROCHLOROTHIAZIDE; VALSARTAN"


def test_annotation_is_canonical_json_of_unii_and_substance_name_only():
    _, drugs, _ = parse.report_rows(_by_id("2004q2", "7547238-2"))
    assert json.loads(drugs[1]["annotation"]) == {
        "substance_name": ["CHOLECALCIFEROL", "NIACINAMIDE", "RIBOFLAVIN"],
        "unii": ["1C6V77QF41", "25X51I8RD4", "TLM2976OFR"],
    }
    # The order of the source lists does not change the stored text.
    a = {"medicinalproduct": "P", "openfda": {"unii": ["B", "A"], "rxcui": ["1"]}}
    b = {"medicinalproduct": "P", "openfda": {"unii": ["A", "B"], "brand_name": ["Z"]}}
    rows = parse.report_rows({"safetyreportid": "1", "patient": {"drug": [a, b]}})[1]
    assert rows[0]["annotation"] == rows[1]["annotation"] == '{"unii":["A","B"]}'


def test_multi_drug_report_gives_one_row_per_drug_with_role():
    _, drugs, _ = parse.report_rows(_by_id("2026q2", "26933255"))
    assert [(d["seq"], d["role"], d["product_name"]) for d in drugs] == [
        (1, "suspect", "DUPIXENT"),
        (2, "concomitant", "ALBUTEROL SULFATE"),
        (3, "concomitant", "ATORVASTATIN CALCIUM"),
    ]
    assert {d["safetyreportid"] for d in drugs} == {"26933255"}


def test_interacting_and_unknown_roles():
    report = {
        "safetyreportid": "1",
        "patient": {"drug": [{"drugcharacterization": "3"}, {"drugcharacterization": "9"}, {}]},
    }
    assert [d["role"] for d in parse.report_rows(report)[1]] == ["interacting", None, None]


def test_batch_number_kept_raw_and_normalised():
    report = {
        "safetyreportid": "1",
        "patient": {
            "drug": [
                {"drugbatchnumb": " ab-123/x "},
                {"medicinalproduct": "no batch"},
                {"drugbatchnumb": "--"},
            ]
        },
    }
    drugs = parse.report_rows(report)[1]
    assert drugs[0]["batch_number"] == "ab-123/x" and drugs[0]["batch_norm"] == "AB123X"
    assert drugs[1]["batch_number"] is None and drugs[1]["batch_norm"] is None
    assert drugs[2]["batch_number"] == "--" and drugs[2]["batch_norm"] is None
    real = parse.report_rows(_by_id("2026q2", "26933255"))[1][0]
    assert real["batch_number"] == "5F841A" and real["batch_norm"] == "5F841A"


def test_route_and_indication_are_kept_as_published():
    _, drugs, _ = parse.report_rows(_by_id("2012q2", "8486749-2"))
    assert drugs[0]["route"] == "042"
    assert drugs[0]["indication"] == "RELAPSING-REMITTING MULTIPLE SCLEROSIS"


# ---------------------------------------------------------------------------
# Reactions and report fields
# ---------------------------------------------------------------------------
def test_reactions_keep_the_term_as_published_and_the_outcome_code():
    _, _, old = parse.report_rows(_by_id("2004q2", "4435605-5"))
    assert [(r["seq"], r["term"], r["outcome"]) for r in old] == [
        (1, "ERYTHEMA", None),
        (2, "FATIGUE", None),
    ]
    _, _, new = parse.report_rows(_by_id("2026q2", "26905497"))
    assert [(r["term"], r["outcome"]) for r in new] == [
        ("Corneal opacity", "6"),
        ("Corneal deposits", "3"),
    ]


def test_version_is_absent_in_the_old_era_and_read_in_the_new():
    assert parse.report_rows(_by_id("2004q2", "4435605-5"))[0]["version"] is None
    assert parse.report_rows(_by_id("2026q2", "26882264"))[0]["version"] == 3


@pytest.mark.parametrize(
    ("report", "country"),
    [
        ({"occurcountry": "DE", "primarysourcecountry": "US"}, "DE"),
        ({"primarysourcecountry": "US", "primarysource": {"reportercountry": "FR"}}, "US"),
        ({"primarysource": {"reportercountry": "UNITED KINGDOM"}}, "UNITED KINGDOM"),
        ({"primarysource": None}, None),  # seen in the 2004q2 sample
        ({}, None),
    ],
)
def test_country_order(report, country):
    assert parse.report_rows({"safetyreportid": "1", **report})[0]["country"] == country


@pytest.mark.parametrize(
    ("age", "unit", "years"),
    [
        ("62", "801", 62.0),
        ("7", "800", 70.0),
        ("6", "802", 0.5),
        ("365.25", "804", 1.0),
        ("62", None, None),
        ("62", "999", None),
        ("abc", "801", None),
        ("-1", "801", None),
        ("1e300", "801", None),
    ],
)
def test_age_is_converted_to_years(age, unit, years):
    patient = {"patientonsetage": age}
    if unit:
        patient["patientonsetageunit"] = unit
    got = parse.report_rows({"safetyreportid": "1", "patient": patient})[0]["age_years"]
    assert got == (pytest.approx(years) if years is not None else None)


def test_sex_and_weight():
    row, _, _ = parse.report_rows(_by_id("2026q2", "26882264"))
    assert row["sex"] == "1"
    assert row["weight_kg"] == pytest.approx(95.238)


def test_nul_is_stripped_and_empty_text_becomes_none():
    report = {
        "safetyreportid": "1\x00",
        "occurcountry": "  ",
        "patient": {
            "drug": [{"medicinalproduct": "AB\x00C", "drugindication": ""}],
            "reaction": [{"reactionmeddrapt": "X\x00Y"}],
        },
    }
    row, drugs, reactions = parse.report_rows(report)
    assert row["safetyreportid"] == "1" and row["country"] is None
    assert drugs[0]["product_name"] == "ABC" and drugs[0]["indication"] is None
    assert reactions[0]["term"] == "XY"


def test_the_dictionary_separator_cannot_appear_in_stored_text():
    drugs = parse.report_rows(
        {"safetyreportid": "1", "patient": {"drug": [{"medicinalproduct": "A\x1fB"}]}}
    )[1]
    assert drugs[0]["product_name"] == "A B"


def test_lone_surrogates_are_removed():
    drugs = parse.report_rows(
        {"safetyreportid": "1", "patient": {"drug": [{"medicinalproduct": "A\ud83dB"}]}}
    )[1]
    assert drugs[0]["product_name"] == "AB"


def test_a_single_drug_object_counts_as_a_list():
    report = {"safetyreportid": "1", "patient": {"drug": {"medicinalproduct": "X"}}}
    assert [d["product_name"] for d in parse.report_rows(report)[1]] == ["X"]


def test_report_without_patient_gives_no_child_rows():
    row, drugs, reactions = parse.report_rows({"safetyreportid": "1"})
    assert row["safetyreportid"] == "1" and drugs == [] and reactions == []


def test_report_without_an_id_is_unusable():
    assert parse.report_rows({"receivedate": "20200101"})[0]["safetyreportid"] is None


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------
def test_reports_stream_from_the_zip_without_a_whole_file_list(tmp_path, monkeypatch):
    # A valid start, then a cut in the JSON far past the reader's buffer. A reader
    # that loads the whole document would fail before it gives the first report.
    pad = "x" * (1024 * 1024)
    first = json.dumps({"safetyreportid": "first", "pad": pad})
    doc = '{"meta": {}, "results": [' + first + ', {"safetyreportid": "cut'
    path = tmp_path / "cut.json.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("drug-event-0001-of-0001.json", doc)

    def boom(*a, **k):
        raise AssertionError("the whole file went through json")

    monkeypatch.setattr(json, "loads", boom)
    monkeypatch.setattr(json, "load", boom)
    stream = parse.iter_reports(path)
    assert next(stream)["safetyreportid"] == "first"
    with pytest.raises(Exception):  # noqa: B017 - the JSON library's own error type
        list(stream)


def test_iter_reports_reads_every_result_in_order(tmp_path):
    reports = _reports("2004q2") + _reports("2026q2")
    path = _zip(tmp_path / "a.json.zip", reports)
    assert [r["safetyreportid"] for r in parse.iter_reports(path)] == [
        r["safetyreportid"] for r in reports
    ]


def test_iter_reports_refuses_a_zip_with_several_json_members(tmp_path):
    path = tmp_path / "two.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("a.json", "{}")
        zf.writestr("b.json", "{}")
    with pytest.raises(ValueError, match="one .json member"):
        list(parse.iter_reports(path))
