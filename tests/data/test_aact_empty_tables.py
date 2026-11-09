"""AACT: a table the file may ship empty does not fail the row check. No DB."""

from __future__ import annotations

from episteme.data.aact import load_aact


def test_search_results_may_be_empty():
    state = {"studies": {"rows": 5}, "search_results": {"rows": 0}}
    assert load_aact.empty_tables(state) == []


def test_any_other_empty_table_still_fails():
    state = {"studies": {"rows": 5}, "search_results": {"rows": 0}, "outcomes": {"rows": 0}}
    assert load_aact.empty_tables(state) == ["outcomes"]
