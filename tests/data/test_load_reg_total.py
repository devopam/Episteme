"""EMA ``meta.total_records`` check: distinct record keys after exact duplicates. No DB."""

from __future__ import annotations

import json

import pytest

from episteme.data.regulatory import load_reg
from episteme.data.regulatory.feeds import Feed


def _feed() -> Feed:
    return Feed(
        name="ema_test",
        url_key="K",
        format="ema_json",
        jurisdiction="EU",
        record_key=("url",),
        substance_fields=("sub",),
        product_fields=("prod",),
        date_field="date",
        cadence="monthly",
    )


def _check(tmp_path, rows: list[dict], total: int):
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"meta": {"total_records": total}, "data": rows}), encoding="utf-8")
    prep = load_reg.RowPreparer(_feed())
    for r in rows:
        prep.prepare(r, "a.json")
    return load_reg._check_counts(_feed(), [f], prep)


def test_non_exact_repeat_counts_once_against_the_total(tmp_path):
    # 251 rows: 204 distinct keys; one key repeats with other content, 46 exact repeats
    rows = [{"url": f"u{i}", "x": 1} for i in range(204)]
    rows.append({"url": "u0", "x": 2})  # non-exact repeat
    rows += [dict(rows[i]) for i in range(1, 47)]  # exact duplicates
    assert len(rows) == 251
    assert _check(tmp_path, rows, total=204) == 204


def test_a_short_feed_still_fails(tmp_path):
    rows = [{"url": f"u{i}"} for i in range(3)]
    with pytest.raises(load_reg._Invalid, match="meta.total_records"):
        _check(tmp_path, rows, total=5)
