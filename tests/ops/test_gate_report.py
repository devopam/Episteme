import json

import polars as pl
import pytest

from episteme.ops import gate, gate_report, profile


def _write(path, rows):
    pl.DataFrame(rows).write_parquet(path)


def _rows():
    return [
        {
            "id": "a",
            "text": "x" * 100,
            "title": "t1",
            "abstract": "ab",
            "extract_status": "ok",
            "license": "CC BY",
            "subset": "commercial",
            "content_hash": "h1",
        },
        {
            "id": "b",
            "text": "y" * 300,
            "title": "t2",
            "abstract": None,
            "extract_status": "ok",
            "license": "CC BY",
            "subset": "commercial",
            "content_hash": "h2",
        },
        {
            "id": "b",
            "text": None,
            "title": None,
            "abstract": None,
            "extract_status": "empty",
            "license": "unknown",
            "subset": "open_metadata",
            "content_hash": "h2",
        },
        {
            "id": "d",
            "text": "z" * 50,
            "title": "t4",
            "abstract": "",
            "extract_status": "partial",
            "license": None,
            "subset": "other",
            "content_hash": None,
        },
    ]


@pytest.fixture
def staging(tmp_path):
    d = tmp_path / "staging"
    d.mkdir()
    _write(d / "s1.parquet", _rows()[:2])
    _write(d / "s2.parquet", _rows()[2:])
    inp = tmp_path / "in"
    inp.mkdir()
    (inp / "f1.xml").write_bytes(b"a" * 400)
    (inp / "f2.xml").write_bytes(b"b" * 600)
    return d, [inp / "f1.xml", inp / "f2.xml"]


def test_counts_percentages_and_yield(staging):
    d, inputs = staging
    m = gate_report.measure_staging(d, inputs=inputs)
    assert m["rows"] == 4
    assert m["inputs"] == 2
    assert m["input_bytes"] == 1000
    assert m["rows_per_input"] == 2.0
    assert m["text_bytes"] == 450
    assert m["text_bytes_per_input_byte"] == 0.45
    assert m["extract_status"] == {"empty": 1, "ok": 2, "partial": 1}
    assert m["extract_status_pct"] == {"empty": 25.0, "ok": 50.0, "partial": 25.0}
    assert m["empty_text_pct"] == 25.0
    assert m["null_pct"]["text"] == 25.0
    assert m["null_pct"]["abstract"] == 75.0
    assert m["null_pct"]["doi"] == 100.0  # column absent from the shards
    assert m["license_counts"] == {"CC BY": 2, "null": 1, "unknown": 1}
    assert m["subset_counts"]["commercial"] == 2
    assert m["text_length"]["text"] == {"p50": 100, "p99": 300, "max": 300}
    assert "failed_inputs" not in m


def test_duplicates(staging):
    d, inputs = staging
    m = gate_report.measure_staging(d, inputs=inputs)
    assert m["duplicate_ids"] == 1
    assert m["duplicate_content_hash"] == 1


def test_distinct_ids_and_duplicates_inside_one_shard(staging):
    """F1: id "b" is in s1 and s2 (a cross-shard re-issue, which the loader keeps
    once); a second "a" inside s1 is a duplicate the loader cannot load."""
    d, inputs = staging
    m = gate_report.measure_staging(d, inputs=inputs)
    assert m["distinct_ids"] == 3
    assert m["duplicate_ids_in_shard"] == 0
    _write(d / "s1.parquet", [*_rows()[:2], _rows()[0]])
    m = gate_report.measure_staging(d, inputs=inputs)
    assert m["duplicate_ids"] == 2
    assert m["distinct_ids"] == 3
    assert m["duplicate_ids_in_shard"] == 1


@pytest.mark.parametrize("column", ["license", "extract_status"])
def test_a_missing_grouped_column_is_a_missing_measure(tmp_path, column):
    """PR review: staging without ``license`` or ``extract_status`` must fail
    the rule that reads it, not pass on an empty dict."""
    d = tmp_path / "staging"
    d.mkdir()
    _write(d / "s1.parquet", [{k: v for k, v in r.items() if k != column} for r in _rows()])
    inp = tmp_path / "f1.xml"
    inp.write_bytes(b"a" * 400)
    m = gate_report.measure_staging(d, inputs=[inp])
    rules = {"allowed_licenses": ["CC BY", "unknown"], "max_status_pct": {"partial": 50.0}}
    fails = gate.evaluate(m, rules)
    rule = "allowed_licenses" if column == "license" else "max_status_pct"
    assert any(f.startswith(rule) and "missing" in f for f in fails), fails


def test_jsonl_only(tmp_path):
    d = tmp_path / "staging"
    d.mkdir()
    (d / "s3.jsonl").write_text(
        json.dumps({"id": "e", "text": "q" * 10, "extract_status": "ok"}) + "\n",
        encoding="utf-8",
    )
    m = gate_report.measure_staging(d, inputs=[])
    assert m["rows"] == 1
    assert m["text_bytes"] == 10


def test_mixed_formats_raise(staging):
    d, inputs = staging
    (d / "s3.jsonl").write_text('{"id": "e"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="mixes .parquet and .jsonl shards"):
        gate_report.measure_staging(d, inputs=inputs)


def test_empty_staging_is_zero_rows(tmp_path):
    d = tmp_path / "staging"
    d.mkdir()
    m = gate_report.measure_staging(d, inputs=[])
    assert m["rows"] == 0
    assert m["duplicate_ids"] == 0
    assert m["distinct_ids"] == 0
    assert m["duplicate_ids_in_shard"] == 0
    assert m["rows_per_input"] is None
    assert m["text_bytes_per_input_byte"] is None
    assert m["null_pct"]["id"] == 100.0


def test_zero_row_shard(tmp_path):
    d = tmp_path / "staging"
    d.mkdir()
    pl.DataFrame({"id": [], "text": []}, schema={"id": pl.Utf8, "text": pl.Utf8}).write_parquet(
        d / "s.parquet"
    )
    m = gate_report.measure_staging(d, inputs=[])
    assert m["rows"] == 0
    assert m["null_pct"]["id"] == 100.0


@pytest.mark.parametrize(
    "record, expect",
    [
        ({"elapsed_s": 2.0, "rc": 0, "peak_rss_mb": 50}, 0),
        ({"elapsed_s": 2.0, "rc": 1, "peak_rss_mb": 50}, None),
        ({"elapsed_s": 2.0, "rc": 1, "peak_rss_mb": 50, "failed_inputs": 3}, 3),
    ],
)
def test_record_merge(staging, record, expect):
    d, inputs = staging
    m = gate_report.measure_staging(d, inputs=inputs, record=record)
    assert m["elapsed_s"] == 2.0
    assert m["peak_rss_mb"] == 50
    assert m["rows_per_s"] == 2.0
    assert m["failed_inputs"] == expect


def test_names_line_up_with_gate_evaluate(staging):
    d, inputs = staging
    m = gate_report.measure_staging(
        d, inputs=inputs, record={"elapsed_s": 1.0, "rc": 0, "peak_rss_mb": 10}
    )
    m.update({"orphan_bodies": 0, "rows_loaded_equals_staging": True})
    rules = {
        "min_rows": 4,
        "min_rows_per_input": 2,
        "min_text_bytes_per_input_byte": 0.4,
        "max_duplicate_ids": 1,
        "max_empty_text_pct": 25,
        "max_status_pct": {"empty": 25, "partial": 25},
        "max_null_pct": {"text": 25},
        "allowed_licenses": ["CC BY", "unknown", "null"],
        "max_failed_inputs": 0,
        "max_orphan_bodies": 0,
        "rows_loaded_equals_staging": True,
    }
    assert gate.evaluate(m, rules) == []
    tight = dict(rules, max_duplicate_ids=0, max_status_pct={"empty": 10})
    assert len(gate.evaluate(m, tight)) == 2


def test_write_report(tmp_path):
    out = gate_report.write_report({"rows": 1, "k": {"a": 1}}, ["min_rows: bad"], tmp_path / "o")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["passed"] is False and data["failures"] == ["min_rows: bad"]
    assert data["measures"]["rows"] == 1
    md = (tmp_path / "o" / "gate_report.md").read_text(encoding="utf-8")
    assert "FAIL" in md and "min_rows: bad" in md


@pytest.mark.pg
def test_measure_load_orphans(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('episteme.article_body')")
        if cur.fetchone()[0] is None:
            pytest.skip("episteme_test is not migrated")
        src = "gate_test_src"
        for i in ("1", "2"):
            cur.execute(
                "INSERT INTO episteme.articles (id, source, source_file) VALUES (%s, %s, %s)",
                (f"g{i}", src, "f.parquet"),
            )
            cur.execute(
                "INSERT INTO episteme.article_body (article_id, source, year, text) "
                "VALUES (%s, %s, 0, 'x')",
                (f"g{i}", src),
            )
        cur.execute(
            "INSERT INTO episteme.article_body (article_id, source, year, text) "
            "VALUES ('orphan', %s, 0, 'x')",
            (src,),
        )
    try:
        m = gate_report.measure_load(pg_conn, src, ["f.parquet"], staging_rows=2)
        assert m == {"rows_loaded": 2, "orphan_bodies": 1, "rows_loaded_equals_staging": True}
        m = gate_report.measure_load(pg_conn, src, ["f.parquet"], staging_rows=3)
        assert m["rows_loaded_equals_staging"] is False
    finally:
        pg_conn.rollback()


def test_a_rare_empty_text_is_not_rounded_away(tmp_path):
    """PR #86 review: one empty text in 25,000 rows is 0.004 %; rounding to two
    decimals made it 0.0, so ``max_empty_text_pct: 0`` passed."""
    d = tmp_path / "staging"
    d.mkdir()
    rows = [{"id": str(i), "text": "x", "extract_status": "ok"} for i in range(24_999)]
    rows.append({"id": "last", "text": "", "extract_status": "partial"})
    _write(d / "s1.parquet", rows)
    inp = tmp_path / "f1.xml"
    inp.write_bytes(b"a" * 30_000)
    m = gate_report.measure_staging(d, inputs=[inp])
    assert m["empty_text_pct"] > 0
    assert m["extract_status_pct"]["partial"] > 0
    fails = gate.evaluate(m, {"max_empty_text_pct": 0, "max_status_pct": {"partial": 0}})
    assert any(f.startswith("max_empty_text_pct") for f in fails), fails
    assert any(f.startswith("max_status_pct") for f in fails), fails


def test_a_yield_just_below_the_floor_is_not_rounded_up(tmp_path):
    """A minimum rule compares the exact ratio, not a rounded one."""
    d = tmp_path / "staging"
    d.mkdir()
    _write(d / "s1.parquet", [{"id": "a", "text": "x" * 1_999_999}])
    inp = tmp_path / "f1.xml"
    inp.write_bytes(b"a" * 100_000_000)
    m = gate_report.measure_staging(d, inputs=[inp])
    assert m["text_bytes_per_input_byte"] < 0.02
    fails = gate.evaluate(m, {"min_text_bytes_per_input_byte": 0.02})
    assert any(f.startswith("min_text_bytes_per_input_byte") for f in fails), fails
    # The failure line shows the exact value, not one that rounds to the limit.
    assert any(str(m["text_bytes_per_input_byte"]) in f for f in fails), fails


def test_the_markdown_report_rounds_for_display(tmp_path):
    gate_report.write_report({"empty_text_pct": 100 / 3, "rows": 3}, [], tmp_path)
    md = (tmp_path / "gate_report.md").read_text(encoding="utf-8")
    assert "| empty_text_pct | 33.3333 |" in md


def test_measure_staging_lists_source_files_and_part_maxima(tmp_path):
    _write(
        tmp_path / "s1.parquet",
        [{"id": f"a{i}", "text": "x" * 10, "source_file": "k__part00001"} for i in range(3)],
    )
    _write(
        tmp_path / "s2.parquet",
        [{"id": "b0", "text": None, "source_file": "k__part00002"}],
    )
    m = gate_report.measure_staging(tmp_path, inputs=[])
    assert m["source_files"] == ["k__part00001", "k__part00002"]
    assert m["max_rows_per_source_file"] == 3
    assert m["max_est_bytes_per_source_file"] == 3 * 10 + 3 * profile.PART_ROW_OVERHEAD_BYTES


def test_measure_staging_part_maxima_none_without_rows(tmp_path):
    m = gate_report.measure_staging(tmp_path, inputs=[])
    assert m["source_files"] == []
    assert m["max_rows_per_source_file"] is None
    assert m["max_est_bytes_per_source_file"] is None
