"""Gate measures of the sources that have no ``articles`` staging (ema, openfda,
openfda_faers): the pure parts here, the SQL parts in the pg-marked tests at the end."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from episteme.ops import gate, gate_report


def test_coverage_pct_counts_distinct_names():
    assert gate_report.coverage_pct(["a", "b", "a", "c"], {"a", "c"}) == pytest.approx(100 * 2 / 3)
    assert gate_report.coverage_pct(["a"], set()) == 0.0
    # No name at all: nothing to measure, so no value (the rule then fails, not passes).
    assert gate_report.coverage_pct([], {"a"}) is None


def test_meta_totals_compare_each_feed_with_its_own_total():
    assert gate_report.meta_total_equals_rows({"a": (10, 10), "b": (4, 4)}) is True
    assert gate_report.meta_total_equals_rows({"a": (10, 10), "b": (3, 4)}) is False
    assert gate_report.meta_total_equals_rows({"a": (10, None)}) is False  # no total declared
    assert gate_report.meta_total_equals_rows({}) is None  # nothing checked


def test_source_measures_are_per_source():
    assert set(gate_report.SOURCE_MEASURES) == {"ema", "openfda", "openfda_faers"}
    # Article measures stay as they were.
    assert callable(gate_report.measure_staging) and callable(gate_report.measure_load)


def test_temp_bytes_of_the_run_is_the_change_between_two_snapshots():
    assert (
        gate_report.temp_bytes_run({"db_temp_bytes_before": 100, "db_temp_bytes_after": 350}) == 250
    )
    assert gate_report.temp_bytes_run({"db_temp_bytes_before": 5, "db_temp_bytes_after": 5}) == 0
    # A reset of the statistics between the snapshots cannot give a negative use.
    assert gate_report.temp_bytes_run({"db_temp_bytes_before": 9, "db_temp_bytes_after": 2}) == 0
    assert (
        gate_report.temp_bytes_run({"db_temp_bytes_before": None, "db_temp_bytes_after": 2}) is None
    )
    assert gate_report.temp_bytes_run({}) is None


def test_gate_run_snapshots_the_temp_counter_for_faers_only(tmp_path, monkeypatch):
    import sys

    from episteme.ops import gate as gate_mod

    values = iter([1000, 4000])
    monkeypatch.setattr(gate_mod, "_db_temp_bytes", lambda: next(values))
    monkeypatch.setattr(gate_mod, "code_hash", lambda s, st: "c")
    monkeypatch.setattr(gate_mod, "rules_hash", lambda s: "r")
    rec = tmp_path / "rec.json"
    rc = gate_mod.main(
        [
            "run",
            "--record",
            str(rec),
            "--source",
            "openfda_faers",
            "--stage",
            "load",
            "--",
            sys.executable,
            "-c",
            "pass",
        ]
    )
    data = json.loads(rec.read_text(encoding="utf-8"))
    assert rc == 0
    assert (data["db_temp_bytes_before"], data["db_temp_bytes_after"]) == (1000, 4000)
    assert gate_report.temp_bytes_run(data) == 3000
    # Another source gets no snapshot.
    rec2 = tmp_path / "rec2.json"
    gate_mod.main(
        [
            "run",
            "--record",
            str(rec2),
            "--source",
            "ema",
            "--stage",
            "load",
            "--",
            sys.executable,
            "-c",
            "pass",
        ]
    )
    assert "db_temp_bytes_before" not in json.loads(rec2.read_text(encoding="utf-8"))


def test_run_record_measures():
    rec = {"elapsed_s": 12.5, "rc": 0, "peak_rss_mb": 321.0}
    assert gate_report.record_measures(rec) == {
        "elapsed_s": 12.5,
        "peak_rss_mb": 321.0,
        "failed_inputs": 0,
    }
    assert gate_report.record_measures({**rec, "rc": 1})["failed_inputs"] is None
    assert gate_report.record_measures({**rec, "rc": 1, "failed_inputs": 2})["failed_inputs"] == 2


def test_a_load_that_did_nothing_has_no_rows_and_fails_the_rule():
    """A sample load whose fingerprint or marker said "current" writes no audit row. The
    measure ``rows`` is then 0 and ``min_rows`` fails; with no run id it is missing."""
    rules = gate.load_rules("openfda_faers")["load"]
    measures = {"rows": 0, "failed_inputs": 0, "peak_rss_mb": 10, "xref_coverage_pct": {}}
    assert any("min_rows" in f for f in gate.evaluate(measures, rules))
    measures.pop("rows")
    assert any("min_rows" in f for f in gate.evaluate(measures, rules))


# ---------------------------------------------------------------------------
# SQL parts. Run only against episteme_test (skipped without TEST_PG_DSN).
# ---------------------------------------------------------------------------


def _migrated(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('episteme.reg_record'), to_regclass('episteme.faers_report')"
        )
        return all(v is not None for v in cur.fetchone())


@pytest.mark.pg
def test_measure_reg_counts_the_latest_release_and_the_names(pg_conn, tmp_path, monkeypatch):
    if not _migrated(pg_conn):
        pytest.skip("episteme_test is not migrated to 0010")
    from episteme.data.regulatory import load_reg

    raw = tmp_path / "ema"
    raw.mkdir()
    feed = raw / "ema_medicines.json"
    feed.write_text(json.dumps({"meta": {"total_records": 2}, "data": []}), encoding="utf-8")
    key = load_reg.input_keys([feed], raw)[0]
    dataset, release = "ema_medicines", "2099-01"
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM episteme.reg_release WHERE dataset = %s", (dataset,))
        cur.execute("DELETE FROM episteme.substance_xref WHERE source = 'gate_test'")
        cur.execute(
            "INSERT INTO episteme.reg_release (dataset, release_id, rows, status, is_latest) "
            "VALUES (%s, %s, 2, 'loaded', true)",
            (dataset, release),
        )
        cur.execute(
            "SELECT to_regclass(%s)", (f"episteme.{load_reg.partition_names(dataset, release)[1]}",)
        )
        if cur.fetchone()[0] is None:
            load_reg._create_partitions(cur, dataset, release)
        cur.execute(
            "INSERT INTO episteme.reg_record "
            "(dataset, release_id, record_key, substance_names, payload, source_file) VALUES "
            "(%s, %s, 'k1', ARRAY['Aspirin'], '{}', %s), "
            "(%s, %s, 'k2', ARRAY['Unknownium'], '{}', %s)",
            (dataset, release, key, dataset, release, key),
        )
        cur.execute(
            "INSERT INTO episteme.substance_xref "
            "(substance_key, id_type, id_value, method, source) "
            "VALUES (-1, 'name', 'aspirin', 'test', 'gate_test')"
        )
    try:
        m = gate_report.measure_reg(
            pg_conn, "ema", raw_dir=raw, run_id=None, record={"rc": 0, "peak_rss_mb": 1.0}
        )
        assert m["meta_total_equals_rows"] is True
        assert m["xref_coverage_pct"][dataset] == pytest.approx(50.0)
        assert m["rows"] is None  # no run id: the rule min_rows fails
        assert m["rows_by_dataset"][dataset] == 2
    finally:
        pg_conn.rollback()


@pytest.mark.pg
def test_measure_faers_reads_the_live_generation(pg_conn):
    if not _migrated(pg_conn):
        pytest.skip("episteme_test is not migrated to 0010")
    try:
        m = gate_report.measure_faers(pg_conn, raw_dir=Path("."), run_id=None, record={"rc": 0})
        for key in (
            "faers_reports",
            "duplicate_ids",
            "largest_year",
            "xref_coverage_pct",
            "db_temp_bytes_run",
            "counts",
        ):
            assert key in m
        assert set(m["xref_coverage_pct"]) <= {"faers_drug_entry"}
    finally:
        pg_conn.rollback()
