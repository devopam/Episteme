"""scripts/data/regulatory/check_plans.py: the plan readers and the six checks, on canned
``EXPLAIN (FORMAT JSON)`` output. The pg-marked test runs it against episteme_test."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "data" / "regulatory" / "check_plans.py"


@pytest.fixture(scope="module")
def cp():
    spec = importlib.util.spec_from_file_location("check_plans", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_plans"] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("check_plans", None)


def scan(relation: str, index: str | None = None) -> dict:
    node = {"Node Type": "Index Scan" if index else "Seq Scan", "Relation Name": relation}
    if index:
        node["Index Name"] = index
    return node


def plan(*nodes: dict) -> list[dict]:
    """EXPLAIN (FORMAT JSON) output: an Append over ``nodes``."""
    return [{"Plan": {"Node Type": "Append", "Plans": list(nodes)}}]


@pytest.fixture
def params(cp):
    return cp.Params(
        dataset="fda_ndc",
        release_id="2026-11",
        moiety_key=7,
        other_moiety_key=8,
        names=("aspirin",),
        window_a=(date(2023, 1, 1), date(2024, 1, 1)),
        window_b=(date(2024, 1, 1), date(2026, 1, 1)),
        years=(2025, 2026),
    )


def test_plan_readers_walk_nested_nodes(cp):
    p = [
        {
            "Plan": {
                "Node Type": "Nested Loop",
                "Plans": [
                    scan("faers_drug_y2025", "faers_drug_g1_y2025_batched"),
                    {"Node Type": "Hash", "Plans": [scan("faers_entry_substance")]},
                ],
            }
        }
    ]
    assert cp.plan_relations(p) == ["faers_drug_y2025", "faers_entry_substance"]
    assert cp.plan_indexes(p) == ["faers_drug_g1_y2025_batched"]
    # The JSON as text, as a driver may hand it over.
    assert cp.plan_relations(__import__("json").dumps(p)) == cp.plan_relations(p)


def test_a_latest_release_query_must_read_one_partition(cp, params):
    one = plan(scan("reg_record_fda_ndc_a1b2c3_2026_11_deadbeef"))
    assert cp.v_latest_release(one, params) == []
    # Pruned to nothing is not "one partition": the release does not exist.
    none = plan()
    problems = cp.v_latest_release(none, params)
    assert problems and "no reg_record partition" in problems[0] and "2026-11" in problems[0]
    many = plan(
        scan("reg_record_fda_ndc_a1b2c3_2026_10_deadbeef"),
        scan("reg_record_fda_ndc_a1b2c3_2026_11_deadbeef"),
    )
    problems = cp.v_latest_release(many, params)
    assert problems and "2 reg_record partitions" in problems[0]
    # Another dataset's partition counts too: it is another leaf.
    assert cp.v_latest_release(
        plan(scan("reg_record_fda_ndc_a1b2c3_x"), scan("reg_record_ema_pip_ffffff_y")), params
    )


def test_pair_counts_must_read_the_signal_table_not_raw_rows(cp, params):
    good = plan(scan("faers_pair_quarter", "faers_pair_quarter_pkey"))
    assert cp.v_pair_counts(good, params) == []
    raw = plan(scan("faers_drug_y2025"), scan("faers_pair_quarter"))
    assert any("raw FAERS rows" in p for p in cp.v_pair_counts(raw, params))
    assert any("does not read faers_pair_quarter" in p for p in cp.v_pair_counts(plan(), params))


def test_batch_rows_must_prune_by_year_and_use_the_partial_index(cp, params):
    good = plan(
        scan("faers_drug_y2025", "faers_drug_g1_y2025_batched"),
        scan("faers_drug_y2026", "faers_drug_g1_y2026_batched"),
        scan("faers_drug_default", "faers_drug_g1_default_batched"),
    )
    assert cp.v_batch_rows(good, params) == []
    all_years = plan(
        *[scan(f"faers_drug_y{y}", f"faers_drug_g1_y{y}_batched") for y in range(2004, 2027)]
    )
    assert any("partitions" in p for p in cp.v_batch_rows(all_years, params))
    no_index = plan(scan("faers_drug_y2025", "faers_drug_g1_y2025_entry"))
    assert any("partial batch index" in p for p in cp.v_batch_rows(no_index, params))


def test_label_interactions_go_from_label_substance_to_label_section(cp, params):
    good = plan(
        scan("label_substance", "label_substance_substance_key_idx"),
        scan("label_section", "label_section_article_loinc_idx"),
    )
    assert cp.v_label_interactions(good, params) == []
    assert cp.v_label_interactions(plan(scan("label_section")), params)
    text = plan(scan("label_substance"), scan("label_section"), scan("article_body_dailymed"))
    assert any("text tables" in p for p in cp.v_label_interactions(text, params))


def test_xref_view_uses_both_indexes(cp, params):
    good = plan(
        scan("substance", "substance_moiety_key_idx"),
        scan("substance_xref", "substance_xref_substance_key_idx"),
    )
    assert cp.v_substance_xref(good, params) == []
    assert len(cp.v_substance_xref(plan(scan("substance"), scan("substance_xref")), params)) == 2


def test_recalled_lots_join_the_batch_signal_table(cp, params):
    good = plan(scan("recall_lot", "recall_lot_recall_idx"), scan("faers_batch_quarter"))
    assert cp.v_recalled_lots(good, params) == []
    raw = plan(scan("recall_lot"), scan("faers_batch_quarter"), scan("faers_drug_y2025"))
    assert any("raw FAERS rows" in p for p in cp.v_recalled_lots(raw, params))
    assert cp.v_recalled_lots(plan(scan("recall_lot")), params)


def test_there_are_six_checks_with_unique_names(cp):
    assert len(cp.CHECKS) == 6
    assert len({c.name for c in cp.CHECKS}) == 6


def test_the_script_writes_nothing_and_has_no_url():
    import re

    text = SCRIPT.read_text(encoding="utf-8")
    assert "conn.read_only = True" in text
    # Every statement it runs starts with SELECT, EXPLAIN or SET LOCAL; EXPLAIN has no ANALYZE.
    first_words = set(re.findall(r'(?:execute|_scalar)\(\s*(?:cur,\s*)?f?"(\w+)', text))
    assert first_words <= {"SELECT", "EXPLAIN", "SET"}, first_words
    assert "SET LOCAL enable_seqscan = off" in text
    assert not re.search(r"EXPLAIN \([^)]*ANALYZE", text)
    assert "http://" not in text and "https://" not in text and "os.environ" not in text


def _params(cp, release_id: str):
    return cp.Params(
        dataset="fda_ndc",
        release_id=release_id,
        moiety_key=1,
        other_moiety_key=2,
        names=("aspirin",),
        window_a=(date(2023, 1, 1), date(2024, 1, 1)),
        window_b=(date(2024, 1, 1), date(2026, 1, 1)),
        years=(2025, 2026),
    )


@pytest.mark.pg
def test_check_1_needs_a_real_release_partition(cp, pg_conn):
    """A real release partition with rows prunes to exactly one leaf; an unknown release
    prunes to nothing and fails with a clear message. All inside one transaction that is
    rolled back."""
    from episteme.data.regulatory import load_reg

    with pg_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('episteme.reg_record')")
        if cur.fetchone()[0] is None:
            pytest.skip("episteme_test is not migrated to 0010")
    release = "2099-gate"
    try:
        with pg_conn.cursor() as cur:
            load_reg._create_partitions(cur, "fda_ndc", release)
            cur.execute(
                "INSERT INTO episteme.reg_record "
                "(dataset, release_id, record_key, substance_names, payload, source_file) "
                "VALUES ('fda_ndc', %s, 'k1', ARRAY['aspirin'], '{}', 'f.zip')",
                (release,),
            )
        plan, _ = cp.explain(pg_conn, cp.q_latest_release(_params(cp, release)))
        assert cp.v_latest_release(plan, _params(cp, release)) == []
        missing = _params(cp, "2099-not-a-release")
        plan, _ = cp.explain(pg_conn, cp.q_latest_release(missing))
        problems = cp.v_latest_release(plan, missing)
        assert problems and "no reg_record partition" in problems[0]
    finally:
        pg_conn.rollback()


@pytest.mark.pg
def test_the_checks_run_on_the_test_database(cp, pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('episteme.faers_pair_quarter'), to_regclass('episteme.reg_record')"
        )
        if not all(cur.fetchone()):
            pytest.skip("episteme_test is not migrated to 0010")
    pg_conn.rollback()
    pg_conn.read_only = True
    results = cp.run_checks(pg_conn, _params(cp, "none"))
    assert len(results) == 6
    for _check, shown, _problems in results:
        assert shown
