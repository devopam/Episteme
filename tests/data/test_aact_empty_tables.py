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
    assert m["upstream_empty_table_count"] == 1


def test_gate_still_counts_a_table_that_lost_its_rows():
    summary = {"rows": 5, "tables": {"studies": 5, "outcomes": 0}, "upstream_empty_tables": []}
    m = gate_report.aact_summary_measures(summary)
    assert m["empty_tables"] == 1 and m["upstream_empty_tables"] == []
    rules = gate.load_rules("aact")["load"]
    assert rules["max_empty_tables"] == 0


def test_a_release_with_no_studies_rows_is_refused():
    import pytest

    state = _state(studies=0, search_results=0)
    with pytest.raises(load_aact._Invalid, match="studies has no rows"):
        load_aact.check_empty(state, ["studies", "search_results"], ["search_results"])


def test_header_only_search_results_is_accepted_by_the_loader():
    state = _state(studies=3, search_results=0)
    load_aact.check_empty(state, ["search_results"], ["search_results"])


def test_a_header_only_table_outside_the_allowlist_fails_the_load():
    import pytest

    state = _state(studies=3, conditions=0)
    with pytest.raises(load_aact._Invalid, match="conditions"):
        load_aact.check_empty(state, ["conditions"], ["search_results"])
    # allowed only when the member is really header-only
    with pytest.raises(load_aact._Invalid, match="search_results"):
        load_aact.check_empty(_state(studies=3, search_results=0), [], ["search_results"])


def _summary(upstream_empty: list[str]) -> dict:
    tables = {f"t{i}": 10 for i in range(47)} | {"studies": 5}
    tables |= {t: 0 for t in upstream_empty}
    return {
        "rows": 5,
        "tables": tables,
        "upstream_empty_tables": upstream_empty,
        "orphans": {},
        "missing_nct_id": [],
    }


def test_gate_passes_with_only_the_allowlisted_table_empty_upstream():
    m = gate_report.aact_summary_measures(_summary(["search_results"]))
    assert m["empty_tables"] == 0 and m["upstream_empty_table_count"] == 1
    assert m["upstream_empty_unlisted"] == []
    rules = gate.load_rules("aact")["load"]
    assert rules["max_upstream_empty_tables"] == 1
    fails = gate.evaluate(
        {**m, "counts": {"tables_with_rows_or_upstream_empty": 49, "studies": 5}}, rules
    )
    assert not any("empty" in f for f in fails)


def test_gate_fails_for_a_header_only_table_that_is_not_allowlisted():
    m = gate_report.aact_summary_measures(_summary(["outcomes"]))
    assert m["upstream_empty_unlisted"] == ["outcomes"]
    assert m["empty_tables"] == 1
    rules = gate.load_rules("aact")["load"]
    fails = gate.evaluate(
        {**m, "counts": {"tables_with_rows_or_upstream_empty": 49, "studies": 5}}, rules
    )
    assert any("max_empty_tables" in f for f in fails)


def test_gate_fails_when_two_tables_are_empty_upstream():
    m = gate_report.aact_summary_measures(_summary(["search_results", "outcomes"]))
    rules = gate.load_rules("aact")["load"]
    fails = gate.evaluate(
        {**m, "counts": {"tables_with_rows_or_upstream_empty": 49, "studies": 5}}, rules
    )
    assert any("max_upstream_empty_tables" in f for f in fails)


def test_the_allowlist_is_a_setting_with_search_results_as_default(monkeypatch):
    from episteme.config import get_settings

    monkeypatch.delenv("EPISTEME_AACT_MAY_BE_EMPTY_TABLES", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().aact_may_be_empty_tables == ("search_results",)
    finally:
        get_settings.cache_clear()


class _ScriptedCursor:
    """Answers the queries of ``measure_aact`` in order: rows of run, release, summary, studies."""

    def __init__(self, answers):
        self.answers = list(answers)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return self.answers.pop(0)


class _ScriptedConn:
    def __init__(self, summary):
        self.cur = _ScriptedCursor([(10,), ("20261103",), (summary,), (5, 5)])

    def cursor(self):
        return self.cur


def test_measure_aact_counts_the_allowlisted_upstream_empty_table_as_met():
    m = gate_report.measure_aact(
        _ScriptedConn(_summary(["search_results"])), raw_dir=Path("."), run_id="r", record={}
    )
    # 47 tables + studies have rows (48); the allowlisted empty table makes 49
    assert m["tables_with_rows"] == 48
    assert m["counts"]["tables_with_rows_or_upstream_empty"] == 49
    rules = gate.load_rules("aact")["load"]
    assert not any("min_counts" in f for f in gate.evaluate({**m, "peak_rss_mb": 1.0}, rules))


def test_measure_aact_does_not_count_an_unlisted_upstream_empty_table():
    m = gate_report.measure_aact(
        _ScriptedConn(_summary(["outcomes"])), raw_dir=Path("."), run_id="r", record={}
    )
    assert m["counts"]["tables_with_rows_or_upstream_empty"] == 48
    assert m["empty_tables"] == 1
