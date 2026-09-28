# tests/data/test_cdisc_ct_parse.py
from pathlib import Path

import pytest

from episteme.data import article_schema
from episteme.data.cdisc_ct.ct_parse import (
    EXPECTED_HEADER,
    CTFormatError,
    build_rows,
    parse_ct_file,
)

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp7" / "cdisc_ct" / "SDTM_Terminology.txt"


def _write(tmp_path, lines):
    p = tmp_path / "X_Terminology.txt"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_parses_codelists_and_terms():
    lists, counts = parse_ct_file(FX)
    by_code = {c.code: c for c in lists}
    assert "C141657" in by_code
    tc = by_code["C141657"]
    assert tc.name == "10-Meter Walk/Run Functional Test Test Code"
    assert tc.extensible == "No"
    assert "TENMW101" in [t.submission_value for t in tc.terms]
    assert counts["skipped_no_id"] == 1
    assert counts["orphan_terms"] == 1


def test_term_before_header_still_attaches():
    lists, _ = parse_ct_file(FX)
    # the fixture places one term row above its codelist header (Step 1)
    assert all(c.terms for c in lists if c.code == "C141657")


def test_double_quotes_do_not_break_columns():
    lists, _ = parse_ct_file(FX)
    defs = [t.definition for c in lists for t in c.terms]
    assert any('"timed up and go"' in d for d in defs)


def test_wrong_header_raises(tmp_path):
    p = _write(tmp_path, ["Code\tName", "C1\tx"])
    with pytest.raises(CTFormatError):
        parse_ct_file(p)


def test_wrong_column_count_raises(tmp_path):
    header = "\t".join(EXPECTED_HEADER)
    p = _write(tmp_path, [header, "C1\t\tNo\tList"])  # truncated row
    with pytest.raises(CTFormatError):
        parse_ct_file(p)


def test_header_only_file_yields_nothing(tmp_path):
    p = _write(tmp_path, ["\t".join(EXPECTED_HEADER)])
    lists, counts = parse_ct_file(p)
    assert lists == [] and counts == {"skipped_no_id": 0, "orphan_terms": 0}


def test_build_rows_ids_licence_and_text():
    lists, _ = parse_ct_file(FX)
    rows = build_rows(
        lists,
        package="SDTM",
        release_date="2026-09-25",
        source_file="SDTM__2026-09-25__SDTM_Terminology.txt",
    )
    r = next(r for r in rows if r["id"] == "cdisc_ct:SDTM:C141657:p1")
    assert r["source"] == "cdisc_ct"
    assert r["source_record_id"] == "SDTM:C141657:p1"
    assert r["license"] == article_schema.LICENSE_PUBLIC_DOMAIN
    assert r["subset"] == "commercial"
    assert "TENMW101" in r["text"] and "2026-09-25" in r["text"] and "SDTM" in r["text"]
    assert set(r) == set(article_schema.ARTICLE_COLUMNS)


def test_large_codelist_splits_with_repeated_header():
    lists, _ = parse_ct_file(FX)
    big = lists[0]
    big.terms = big.terms * 150  # > 200 terms
    rows = build_rows(
        [big], package="SDTM", release_date="2026-09-25", source_file="f", max_terms=200
    )
    n = -(-len(big.terms) // 200)
    assert [r["id"] for r in rows] == [f"cdisc_ct:SDTM:{big.code}:p{i}" for i in range(1, n + 1)]
    assert all(big.name in r["text"] for r in rows)
    assert all(f"part {i} of {n}" in rows[i - 1]["title"] for i in range(1, n + 1))


def test_same_codelist_in_two_packages_gets_distinct_ids():
    lists, _ = parse_ct_file(FX)
    a = build_rows(lists[:1], package="SDTM", release_date="d", source_file="a")
    b = build_rows(lists[:1], package="SEND", release_date="d", source_file="b")
    assert {r["id"] for r in a}.isdisjoint({r["id"] for r in b})
