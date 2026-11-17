"""The Python mapping layer of the SQL lookup API (non-pg)."""

from __future__ import annotations

from pathlib import Path

import pytest

from episteme.data.regulatory import load_substance
from episteme.reference import _api

_MIGRATION = (
    Path(__file__).resolve().parents[2] / "src/episteme/data/db/migrations/0015_api_layer.sql"
)
_SCHEMA = Path(__file__).resolve().parents[2] / "src/episteme/data/db/schema.sql"


def _row(kind, group=None, rank=None, text=None, value=None, method=None, **kw):
    return (
        kind,
        group,
        rank,
        text,
        value,
        method,
        kw.get("confidence"),
        kw.get("basis"),
        kw.get("source"),
        kw.get("release_id"),
        kw.get("source_file"),
        kw.get("record_key"),
        kw.get("jurisdiction"),
        kw.get("licence"),
    )


def _claim(group, rank, value, method="mapped", confidence=0.5, **kw):
    return _row(
        "claim",
        group,
        rank,
        f"text {value}",
        value,
        method,
        confidence=confidence,
        basis="basis",
        source=kw.get("source", "substance"),
        release_id=kw.get("release_id", "r1"),
        source_file=kw.get("source_file"),
        record_key=f"unii:{value}",
        jurisdiction=kw.get("jurisdiction"),
        licence="commercial",
    )


def test_rows_to_result_alternatives():
    rows = [
        _claim(1, 0, "A"),
        _claim(1, 2, "C"),  # out of order: sorted by alt_rank
        _claim(1, 1, "B"),
        _claim(2, 0, "D", method="fetched", confidence=1.0, source_file="f.json"),
    ]
    res = _api.rows_to_result(rows)
    assert [c.value for c in res.claims] == ["A", "D"]
    first, second = res.claims
    assert first.method == "mapped"
    assert [a.value for a in first.alternatives] == ["B", "C"]
    assert first.confidence == 0.5
    assert first.confidence_basis == "basis"
    assert first.text == "text A"
    assert first.provenance.source == "substance"
    assert first.provenance.release == "r1"
    assert first.provenance.record_key == "unii:A"
    assert second.alternatives == ()
    assert second.provenance.source_file == "f.json"
    assert res.coverage.matched == 2
    assert res.coverage.missing == ()
    assert res.coverage.notes == ()


def test_rows_to_result_notes_missing():
    rows = [
        _row("missing", source="ema_medicines"),
        _row("note", text="truncated: 50 of 60"),
        _claim(1, 0, "A", method="fetched", confidence=1.0),
        _row("missing", source="fda_drugsfda"),
    ]
    res = _api.rows_to_result(rows)
    assert res.coverage.missing == ("ema_medicines", "fda_drugsfda")
    assert res.coverage.notes == ("truncated: 50 of 60",)
    assert res.coverage.matched == 1
    empty = _api.rows_to_result([_row("missing", source="substance")])
    assert empty.claims == ()
    assert empty.coverage.matched == 0
    assert empty.coverage.missing == ("substance",)


def test_rows_to_result_rejects_bad_rows():
    with pytest.raises(ValueError, match="no claim"):
        _api.rows_to_result([_claim(1, 1, "B")])
    with pytest.raises(ValueError, match="kind"):
        _api.rows_to_result([_row("article")])
    with pytest.raises(ValueError, match="columns"):
        _api.rows_to_result([("claim", 1, 0)])
    with pytest.raises(ValueError, match="duplicate"):
        _api.rows_to_result([_claim(1, 0, "A"), _claim(1, 0, "B")])


class _Cursor:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params):
        self.log.append((query, params))

    def fetchall(self):
        return [("row",)]


class _Conn:
    def __init__(self):
        self.log = []

    def cursor(self):
        return _Cursor(self.log)


def test_call_rejects_unknown_function():
    conn = _Conn()
    for name in ("drop_everything", "resolve_entity; DROP TABLE x", "Resolve_Entity", ""):
        with pytest.raises(ValueError, match="unknown API function"):
            _api.call(conn, name, "x")
    assert conn.log == []


def test_call_quotes_name_and_binds_args():
    conn = _Conn()
    assert _api.call(conn, "resolve_entity", "aspirin'; --", None) == [("row",)]
    ((query, params),) = conn.log
    assert query.as_string(None) == (
        'SELECT * FROM "episteme_api_internal"."resolve_entity"(%s, %s)'
    )
    assert params == ("aspirin'; --", None)


def test_internal_functions_listed():
    assert _api.INTERNAL_FUNCTIONS == {
        "resolve_entity",
        "substance_family",
        "approval_status",
        "safety_actions",
        "current_label",
        "label_section_text",
        "trials",
        "faers_counts",
        "literature_by_mesh",
        "graph_neighbours",
        "search_text",
        "lookup_code",
    }


def test_resolve_release_dataset_matches_loader():
    # resolve_entity reads the release of the substance loader's dataset; the SQL
    # hard-codes the name, so it must follow load_substance.DATASET.
    needle = f"rr.dataset = '{load_substance.DATASET}'"
    assert needle in _MIGRATION.read_text(encoding="utf-8")
    assert needle in _SCHEMA.read_text(encoding="utf-8")
