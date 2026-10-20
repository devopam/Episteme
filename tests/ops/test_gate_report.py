import json

import polars as pl
import pytest

from episteme.ops import gate, gate_report


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
