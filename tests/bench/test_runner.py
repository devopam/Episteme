from __future__ import annotations

import json
from datetime import date

from episteme.bench.pbrer_cases import PbrerCase, write_case
from episteme.bench.runner import main, score
from episteme.harness.pbrer import GAP_PREFIX
from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance
from episteme.reference.rules import MAH_MARKER, load_rules


def _case(**kw) -> PbrerCase:
    base = dict(
        case_id="c1",
        substance_key=1,
        substance_name="Metformin",
        start=date(2023, 1, 1),
        end=date(2023, 12, 31),
        psusa_record_key="k",
        expected_facts={"2": ["approved 2001-02-03", "other fact"]},
        expected_gaps={"9": [GAP_PREFIX + "faers"]},
    )
    base.update(kw)
    return PbrerCase(**base)


def _result(text: str, value: str | None = None) -> LookupResult:
    c = Claim(text, value, "fetched", Provenance("s", "r", None, "k1", None), 1.0, "b")
    return LookupResult((c,), Coverage(1))


def _draft() -> dict[str, str]:
    rules = load_rules("ich_e2c_r2")
    return {s.heading: MAH_MARKER if s.mah_data else "text" for s in rules.sections_for("ich")}


def test_score_clean_draft():
    d = _draft()
    first = next(h for h in d if d[h] == "text")
    d[first] = "approved 2001-02-03\n" + GAP_PREFIX + "faers"
    s = score(_case(), d, [_result("approved 2001-02-03")])
    assert s["sections_ok"] is True
    assert s["mah_markers_ok"] is True
    assert s["invented_values"] == 0
    assert s["facts_found"] == 0.5
    assert s["gaps_reported"] == 1.0


def test_score_counts_invented_values():
    d = _draft()
    first = next(h for h in d if d[h] == "text")
    d[first] = "approved 2001-02-03 and 1999-09-09, 12345 reports"
    s = score(_case(), d, [_result("approved 2001-02-03")])
    assert s["invented_values"] == 2


def test_score_flags_missing_section_and_marker():
    d = _draft()
    d.pop(next(iter(d)))
    mah = next(h for h, b in _draft().items() if b == MAH_MARKER)
    d[mah] = "company data invented"
    s = score(_case(), d, [])
    assert s["sections_ok"] is False
    assert s["mah_markers_ok"] is False


def test_score_empty_expectations_count_as_full():
    s = score(_case(expected_facts={}, expected_gaps={}), _draft(), [])
    assert s["facts_found"] == 1.0
    assert s["gaps_reported"] == 1.0


def test_runner_skips_unreviewed_cases(tmp_path):
    cases, out = tmp_path / "cases", tmp_path / "out"
    write_case(_case(case_id="a", reviewed_by_owner=True), cases)
    write_case(_case(case_id="b"), cases)
    seen: list[str] = []

    def produce(case):
        seen.append(case.case_id)
        return _draft(), []

    rc = main(["--cases", str(cases), "--out", str(out)], produce=produce)
    assert rc == 0
    assert seen == ["a"]
    data = json.loads((out / "scores.json").read_text("utf-8"))
    assert data["skipped_unreviewed"] == 1
    assert list(data["cases"]) == ["a"]
    assert list(data["cases"]["a"]) == sorted(data["cases"]["a"])
