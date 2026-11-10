"""AACT: a table that is empty upstream does not fail the row check or the gate. No DB."""

from __future__ import annotations

import zipfile
from pathlib import Path

from episteme.data.aact import load_aact
from episteme.ops import gate, gate_report


def _zip(path: Path, members: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text)
    return path


def _state(**rows: int) -> dict[str, dict]:
    return {t: {"rows": n, "stats": {"member": f"{t}.txt"}} for t, n in rows.items()}


def test_member_has_data_rows_needs_a_record_after_the_header(tmp_path):
    z = _zip(
        tmp_path / "r.zip",
        {
            "search_results.txt": "id|nct_id|name\n",
            "blank_after.txt": "id|nct_id\n\n\n",
            "outcomes.txt": "id|nct_id\n1|NCT1\n",
        },
    )
    assert not load_aact.member_has_data_rows(z, "search_results.txt")
    assert not load_aact.member_has_data_rows(z, "blank_after.txt")
    assert load_aact.member_has_data_rows(z, "outcomes.txt")


def test_a_header_only_member_is_empty_upstream_not_a_failure(tmp_path):
    z = _zip(
        tmp_path / "r.zip",
        {
            "studies.txt": "nct_id\nNCT1\n",
            "search_results.txt": "id|nct_id|name\n",
            "outcomes.txt": "id|nct_id\n1|NCT1\n",
        },
    )
    state = _state(studies=1, search_results=0, outcomes=0)
    upstream = load_aact.upstream_empty_tables(z, state)
    assert upstream == ["search_results"]
    # outcomes has data rows upstream but loaded none: still a failure
    assert load_aact.empty_tables(state, upstream) == ["outcomes"]


def test_any_other_empty_table_still_fails():
    state = _state(studies=5, search_results=0, outcomes=0)
    assert load_aact.empty_tables(state) == ["outcomes", "search_results"]


def test_summary_without_member_name_is_never_upstream_empty(tmp_path):
    z = _zip(tmp_path / "r.zip", {"a.txt": "id\n"})
    assert load_aact.upstream_empty_tables(z, {"a": {"rows": 0, "stats": {}}}) == []


def test_gate_measures_report_upstream_empty_apart_and_floor_holds():
    tables = {f"t{i}": 10 for i in range(48)} | {"studies": 5, "search_results": 0}
    summary = {
        "rows": 5,
        "tables": tables,
        "upstream_empty_tables": ["search_results"],
        "orphans": {},
        "missing_nct_id": [],
    }
    m = gate_report.aact_summary_measures(summary)
    assert m["upstream_empty_tables"] == ["search_results"]
    assert m["empty_tables"] == 0
    assert m["tables_with_rows"] == 49
    assert m["tables_with_upstream_data"] == 49


def test_gate_still_counts_a_table_that_lost_its_rows():
    summary = {"rows": 5, "tables": {"studies": 5, "outcomes": 0}, "upstream_empty_tables": []}
    m = gate_report.aact_summary_measures(summary)
    assert m["empty_tables"] == 1 and m["upstream_empty_tables"] == []
    rules = gate.load_rules("aact")["load"]
    assert rules["max_empty_tables"] == 0
