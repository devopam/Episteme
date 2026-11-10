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
    assert set(gate_report.SOURCE_MEASURES) == {"ema", "openfda", "openfda_faers", "aact"}
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
# AACT (spec 7): the measures of a release load, and the rules over them
# ---------------------------------------------------------------------------
def _aact_summary(**over):
    base = {
        "rows": 1000,
        "tables": {"studies": 1000, "conditions": 2500, "interventions": 1800},
        "orphans": {"conditions": 5, "interventions": 0},
        "names": {"interventions": 1800, "interventions_matched": 900, "matched": 40},
    }
    base.update(over)
    return base


def test_aact_summary_measures_count_tables_orphans_and_coverage():
    m = gate_report.aact_summary_measures(_aact_summary())
    assert m["tables_with_rows"] == 3 and m["empty_tables"] == 0
    assert m["orphan_share_pct"] == {"conditions": pytest.approx(0.2), "interventions": 0.0}
    assert m["orphan_share_pct_max"] == pytest.approx(0.2)
    assert m["xref_coverage_pct"] == {"aact_intervention": pytest.approx(50.0)}
    empty = gate_report.aact_summary_measures(_aact_summary(tables={"studies": 1, "notes": 0}))
    assert empty["tables_with_rows"] == 1 and empty["empty_tables"] == 1


def test_aact_summary_measures_without_a_summary_are_missing():
    """A load that did nothing in this run has no summary: every measure is None, so the
    rules fail instead of passing on an earlier run's data."""
    m = gate_report.aact_summary_measures(None)
    assert m["tables_with_rows"] is None and m["empty_tables"] is None
    assert m["orphan_share_pct_max"] is None
    assert m["xref_coverage_pct"] == {"aact_intervention": None}
    # An empty, missing or partial orphan record is no measure: the rule must fail on it.
    for orphans in ({}, None):
        m = gate_report.aact_summary_measures(_aact_summary(orphans=orphans))
        assert m["orphan_share_pct_max"] is None
        assert any(
            "max_orphan_share_pct" in f
            for f in gate.evaluate(
                {**_aact_measures(), "orphan_share_pct_max": None},
                gate.load_rules("aact")["load"],
            )
        )
    no_orphans = {k: v for k, v in _aact_summary().items() if k != "orphans"}
    assert gate_report.aact_summary_measures(no_orphans)["orphan_share_pct_max"] is None
    partial = gate_report.aact_summary_measures(_aact_summary(orphans={"conditions": 5}))
    assert partial["orphan_share_pct_max"] is None  # interventions was not checked
    # Only studies loaded and no orphan record: still no measure.
    only = gate_report.aact_summary_measures(_aact_summary(tables={"studies": 3}, orphans={}))
    assert only["orphan_share_pct_max"] is None
    # No intervention at all: a share of nothing is not a pass.
    none = gate_report.aact_summary_measures(
        _aact_summary(names={"interventions": 0, "interventions_matched": 0})
    )
    assert none["xref_coverage_pct"] == {"aact_intervention": None}


def test_aact_a_table_without_nct_id_counts_as_checked():
    """``search_terms`` has no ``nct_id``, so the loader cannot count its orphans; the load
    summary lists it in ``missing_nct_id``. Such a table is checked, so the largest share is
    a measure; a table in neither list still leaves the measure missing."""
    tables = {"studies": 1000, "conditions": 2500, "interventions": 1800, "search_terms": 40}
    m = gate_report.aact_summary_measures(
        _aact_summary(tables=tables, missing_nct_id=["search_terms"])
    )
    assert m["orphan_share_pct_max"] == pytest.approx(0.2)
    assert "search_terms" not in m["orphan_share_pct"]
    assert gate.evaluate({**_aact_measures(), **m}, gate.load_rules("aact")["load"]) == []
    # Without the list, search_terms is unchecked: no measure, so the rule fails.
    assert (
        gate_report.aact_summary_measures(_aact_summary(tables=tables))["orphan_share_pct_max"]
        is None
    )
    # A table in neither list still fails, with the list present.
    more = {**tables, "notes": 3}
    m = gate_report.aact_summary_measures(
        _aact_summary(tables=more, missing_nct_id=["search_terms"])
    )
    assert m["orphan_share_pct_max"] is None
    # The list alone, with no orphan record, is no measure.
    m = gate_report.aact_summary_measures(
        _aact_summary(
            tables={"studies": 3, "search_terms": 2}, orphans={}, missing_nct_id=["search_terms"]
        )
    )
    assert m["orphan_share_pct_max"] is None


def _aact_measures(**over):
    base = {
        "rows": 1000,
        "failed_inputs": 0,
        "peak_rss_mb": 900.0,
        "empty_tables": 0,
        "upstream_empty_table_count": 1,
        "orphan_share_pct_max": 0.2,
        "studies_equal_distinct_nct_id": True,
        "xref_coverage_pct": {"aact_intervention": 50.0},
        "counts": {"tables_with_rows_or_upstream_empty": 49, "studies": 609_804},
    }
    base.update(over)
    return base


def test_aact_rules_need_tables_rows_and_orphans_under_limit():
    rules = gate.load_rules("aact")["load"]
    assert gate.evaluate(_aact_measures(), rules) == []
    # A table with no row.
    assert any(
        "max_empty_tables" in f for f in gate.evaluate(_aact_measures(empty_tables=1), rules)
    )
    # Too few tables, and no study.
    for key in ("tables_with_rows_or_upstream_empty", "studies"):
        counts = {**_aact_measures()["counts"], key: 0}
        fails = gate.evaluate(_aact_measures(counts=counts), rules)
        assert any("min_counts" in f and key in f for f in fails), key
    # Studies and distinct nct_id differ.
    fails = gate.evaluate(_aact_measures(studies_equal_distinct_nct_id=False), rules)
    assert any("studies_equal_distinct_nct_id" in f for f in fails)
    # The orphan share limit is 5 percent, as the loader's default.
    assert gate.evaluate(_aact_measures(orphan_share_pct_max=5.0), rules) == []
    fails = gate.evaluate(_aact_measures(orphan_share_pct_max=5.01), rules)
    assert any("max_orphan_share_pct" in f for f in fails)
    # Memory: at most 2048 MB.
    assert gate.evaluate(_aact_measures(peak_rss_mb=2048.0), rules) == []
    assert any("peak_rss" in f for f in gate.evaluate(_aact_measures(peak_rss_mb=2049.0), rules))
    # A load that did nothing in this run fails.
    assert any("min_rows" in f for f in gate.evaluate(_aact_measures(rows=0), rules))
    # Intervention coverage is recorded, not limited: 0 percent passes, no measure fails.
    assert gate.evaluate(_aact_measures(xref_coverage_pct={"aact_intervention": 0.0}), rules) == []
    assert gate.evaluate(_aact_measures(xref_coverage_pct={"aact_intervention": None}), rules)
    # Every rule fails on a missing measure.
    for key in (
        "empty_tables",
        "orphan_share_pct_max",
        "studies_equal_distinct_nct_id",
        "peak_rss_mb",
        "counts",
    ):
        gone = {k: v for k, v in _aact_measures().items() if k != key}
        assert gate.evaluate(gone, rules), key


def test_epar_reports_section_share():
    m = gate_report.section_measures(80, 20)
    assert m == {
        "documents_with_sections": 80,
        "documents_without_sections": 20,
        "sections_share_pct": pytest.approx(80.0),
    }
    # Nothing counted: no share (not a pass, not a zero).
    assert gate_report.section_measures(0, 0)["sections_share_pct"] is None
    assert gate_report.section_measures(None, None)["sections_share_pct"] is None
    # Report only: no rule of ema_epar names the share, so the sample sets the floor later.
    for stage, rules in gate.load_rules("ema_epar").items():
        assert not [k for k in rules if "section" in k], stage
        measures = {"sections_share_pct": 0.0, "documents_with_sections": 0}
        assert not [f for f in gate.evaluate(measures, rules) if "section" in f], stage


def test_gate_reads_the_epar_section_counts_of_the_extract(tmp_path, monkeypatch):
    import episteme.config as cfg
    from episteme.data import bucket_parts

    monkeypatch.setenv("EPISTEME_RUN_ID", "run-1")
    cfg.get_settings.cache_clear()

    processed = tmp_path / "02_processed"
    bucket_parts.write_failed_inputs(
        processed,
        "ema_epar",
        0,
        no_text=1,
        documents_with_sections=3,
        documents_without_sections=1,
    )
    got = gate.epar_section_measures(processed, "run-1")
    assert got["documents_with_sections"] == 3 and got["documents_without_sections"] == 1
    assert got["sections_share_pct"] == pytest.approx(75.0)
    # No file: every value is missing.
    empty = gate.epar_section_measures(tmp_path / "nowhere", "run-1")
    assert empty["documents_with_sections"] is None and empty["sections_share_pct"] is None
    # A file of another run, or no run id, counts for nothing.
    other = gate.epar_section_measures(processed, "run-2")
    assert other["documents_with_sections"] is None and other["sections_share_pct"] is None
    assert gate.epar_section_measures(processed, None)["sections_share_pct"] is None
    cfg.get_settings.cache_clear()


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


@pytest.mark.pg
def test_measure_aact_reads_the_release_summary_and_the_live_studies(pg_conn, monkeypatch):
    if not _migrated(pg_conn):
        pytest.skip("episteme_test is not migrated to 0010")
    monkeypatch.setenv("EPISTEME_ACTOR", "pytest")
    from episteme import audit_trail

    release, run_id = "20990101", "aact-gate-test"
    try:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM episteme.reg_release WHERE dataset = 'aact'")
            cur.execute(
                "INSERT INTO episteme.reg_release (dataset, release_id, rows, status, is_latest) "
                "VALUES ('aact', %s, 2, 'loaded', true)",
                (release,),
            )
            cur.execute("DROP SCHEMA IF EXISTS aact CASCADE")
            cur.execute("CREATE SCHEMA aact")
            cur.execute("CREATE TABLE aact.studies (nct_id text)")
            cur.execute("INSERT INTO aact.studies VALUES ('NCT1'), ('NCT2')")
        audit_trail.record(
            "load_commit",
            conn=pg_conn,
            object=f"aact {release}",
            rows_affected=2,
            new_value=_aact_summary(rows=2),
            run_id=run_id,
        )
        m = gate_report.measure_aact(
            pg_conn, raw_dir=Path("."), run_id=run_id, record={"rc": 0, "peak_rss_mb": 5.0}
        )
        assert m["studies_equal_distinct_nct_id"] is True
        assert m["counts"] == {"tables_with_rows_or_upstream_empty": 3, "studies": 2}
        assert m["orphan_share_pct_max"] == pytest.approx(0.2)
        assert m["rows"] >= 2
        # Another run id has no summary: the measures are missing.
        other = gate_report.measure_aact(pg_conn, raw_dir=Path("."), run_id="x", record={"rc": 0})
        assert other["orphan_share_pct_max"] is None
    finally:
        pg_conn.rollback()
