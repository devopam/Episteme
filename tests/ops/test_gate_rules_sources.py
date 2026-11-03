"""The shipped rules of bookshelf, europepmc_manuscript and apollo: they parse,
use known keys only, fail a fixture shaped like the pre-fix bookshelf output and
pass fixtures shaped like the real 2026-10-20 sample outputs."""

from __future__ import annotations

import pytest

from episteme.ops import gate

SOURCES = ["bookshelf", "europepmc_manuscript", "apollo", "pmc"]


@pytest.mark.parametrize("source", SOURCES)
def test_rules_parse_and_use_known_keys_only(source):
    rules = gate.load_rules(source)
    assert rules is not None
    for stage in ("extract", "load"):
        assert gate.is_gated(source, stage)
        assert not set(rules[stage]) - gate._KNOWN_KEYS


@pytest.mark.parametrize("source", SOURCES)
def test_load_section_requires_no_orphans_and_equal_rows(source):
    load = gate.load_rules(source)["load"]
    assert load["max_orphan_bodies"] == 0
    assert load["rows_loaded_equals_staging"] is True
    # 642.5 MB: the europepmc_manuscript load sample's peak after the byte cap (2026-10-28).
    ok = {"orphan_bodies": 0, "rows_loaded_equals_staging": True, "peak_rss_mb": 642.5}
    assert gate.evaluate(ok, load) == []
    assert gate.evaluate({**ok, "orphan_bodies": 1}, load)
    assert gate.evaluate({**ok, "rows_loaded_equals_staging": False}, load)


def test_europepmc_load_fails_over_its_memory_limit():
    # 2026-10-28: the load reached 19.5 GB before the byte cap; the gate now stops that.
    load = gate.load_rules("europepmc_manuscript")["load"]
    ok = {"orphan_bodies": 0, "rows_loaded_equals_staging": True}
    assert load["max_peak_rss_mb"] == 4096
    assert gate.evaluate({**ok, "peak_rss_mb": 4096}, load) == []
    assert gate.evaluate({**ok, "peak_rss_mb": 19_503}, load)


def _bookshelf(**over):
    base = {
        "rows": 224,
        "rows_per_input": 10.18,
        "text_bytes_per_input_byte": 0.0325,
        "duplicate_ids": 0,
        "empty_text_pct": 0.0,
        "extract_status_pct": {"ok": 96.43, "partial": 3.57},
        "license_counts": {"CC BY-NC": 1, "CC BY-NC-ND": 4, "unknown": 219},
        "failed_inputs": 0,
    }
    base.update(over)
    return base


def test_bookshelf_post_fix_output_passes():
    assert gate.evaluate(_bookshelf(), gate.load_rules("bookshelf")["extract"]) == []


def test_bookshelf_pre_fix_output_fails_on_yield():
    pre = _bookshelf(
        rows=40,
        rows_per_input=1.8182,
        text_bytes_per_input_byte=0.005859,
        extract_status_pct={"ok": 62.5, "partial": 37.5},
        license_counts={"CC BY-NC-ND": 4, "unknown": 36},
    )
    fails = gate.evaluate(pre, gate.load_rules("bookshelf")["extract"])
    text = "\n".join(fails)
    assert "min_rows_per_input" in text
    assert "min_text_bytes_per_input_byte" in text
    assert "max_status_pct" in text


def test_bookshelf_unexpected_license_fails():
    fails = gate.evaluate(
        _bookshelf(license_counts={"text_mining": 3}), gate.load_rules("bookshelf")["extract"]
    )
    assert any("allowed_licenses" in f for f in fails)


def _europepmc(**over):
    base = {
        "rows_per_input": 1353.7,
        "duplicate_ids": 2,
        "duplicate_ids_in_shard": 0,
        "empty_text_pct": 0.0,
        "null_pct": {"abstract": 7.24, "language": 15.32},
        "license_counts": {"text_mining": 4061},
        "failed_inputs": 0,
    }
    base.update(over)
    return base


def test_europepmc_sample_output_passes():
    assert gate.evaluate(_europepmc(), gate.load_rules("europepmc_manuscript")["extract"]) == []


def test_europepmc_low_yield_and_other_license_fail():
    rules = gate.load_rules("europepmc_manuscript")["extract"]
    assert gate.evaluate(_europepmc(rows_per_input=20.0), rules)
    assert gate.evaluate(_europepmc(license_counts={"CC BY": 5}), rules)
    assert gate.evaluate(_europepmc(null_pct={"abstract": 40.0, "language": 1.0}), rules)


def test_europepmc_allows_cross_archive_reissues_but_not_one_inside_an_archive():
    """F1: the 2 duplicate ids of the 2026-10-20 sample sit in two archives (the
    loader keeps one row each); a duplicate inside one archive breaks the load."""
    rules = gate.load_rules("europepmc_manuscript")["extract"]
    assert rules["max_duplicate_ids_in_shard"] == 0
    fails = gate.evaluate(_europepmc(duplicate_ids_in_shard=1), rules)
    assert any("max_duplicate_ids_in_shard" in f for f in fails)


def _apollo(**over):
    base = {
        "rows_per_input": 8335.5,
        "text_bytes_per_input_byte": 0.988,
        "duplicate_ids": 0,
        "empty_text_pct": 0.0,
        "extract_status_pct": {"ok": 99.85, "dropped": 0.15},
        "license_counts": {"permissive": 16671},
        "failed_inputs": 0,
    }
    base.update(over)
    return base


def test_apollo_sample_output_passes_and_bad_yield_fails():
    rules = gate.load_rules("apollo")["extract"]
    assert gate.evaluate(_apollo(), rules) == []
    assert gate.evaluate(_apollo(rows_per_input=3.0), rules)
    assert gate.evaluate(_apollo(text_bytes_per_input_byte=0.01), rules)
    assert gate.evaluate(_apollo(license_counts={"unknown": 5}), rules)


def _pubchem(**over):
    base = {
        "failed_inputs": 0,
        "duplicate_ids": 0,
        "empty_text_pct": 0.0,
        "license_counts": {"public_domain": 30},
        "extract_status_pct": {"ok": 99.7416, "partial": 0.2584},  # the first real sample
        "peak_rss_mb": 600,
        "max_rows_per_source_file": 10,
        "max_est_bytes_per_source_file": 5000,
        "part_rows": 10,
        "part_bytes": 10**6,
    }
    base.update(over)
    return base


def test_pubchem_rules_parse_and_use_known_keys_only():
    rules = gate.load_rules("pubchem")
    assert rules is not None
    for stage in ("serialize", "load"):
        assert gate.is_gated("pubchem", stage)
        assert not set(rules[stage]) - gate._KNOWN_KEYS
    assert not any("rows_per_s" in k for k in rules["serialize"])


def test_pubchem_serialize_rules_pass_and_fail():
    rules = gate.load_rules("pubchem")["serialize"]
    assert gate.evaluate(_pubchem(), rules) == []
    assert gate.evaluate(_pubchem(failed_inputs=1), rules)
    assert gate.evaluate(_pubchem(license_counts={"unknown": 3}), rules)
    assert gate.evaluate(_pubchem(extract_status_pct={"ok": 70.0, "partial": 30.0}), rules)
    # 2.0 %: a missing sibling file (rows without IUPAC, title or mass) fails at once.
    assert gate.evaluate(_pubchem(extract_status_pct={"ok": 97.5, "partial": 2.5}), rules)
    assert gate.evaluate(_pubchem(peak_rss_mb=2000), rules)
    assert gate.evaluate(_pubchem(max_rows_per_source_file=11), rules)


def test_pubchem_load_rules_require_no_orphans_and_equal_rows():
    load = gate.load_rules("pubchem")["load"]
    assert gate.evaluate({"orphan_bodies": 0, "rows_loaded_equals_staging": True}, load) == []
    assert gate.evaluate({"orphan_bodies": 1, "rows_loaded_equals_staging": True}, load)
    assert gate.evaluate({"orphan_bodies": 0, "rows_loaded_equals_staging": False}, load)


# --- the regulatory sources (dailymed, ema_epar, ema, openfda, openfda_faers) ----------

REGULATORY = {
    "dailymed": ("extract", "load"),
    "ema_epar": ("extract", "load"),
    "ema": ("load",),
    "openfda": ("load",),
    "openfda_faers": ("load",),
}


def test_regulatory_sources_are_gated_and_rules_use_known_keys():
    for source, stages in REGULATORY.items():
        rules = gate.load_rules(source)
        assert rules is not None, source
        assert set(rules) == set(stages), source  # a stage the gate cannot hash is refused
        for stage in stages:
            assert gate.is_gated(source, stage), (source, stage)
            assert not set(rules[stage]) - gate._KNOWN_KEYS, (source, stage)
            gate.evaluate({}, rules[stage])  # parses; every rule fails on a missing measure
        # Every gated source has a slice, or `--sample` cannot run.
        from episteme.ops import gate_slice

        assert source in gate_slice.SLICES


def test_regulatory_loads_need_work_in_this_run():
    """A sample load that did nothing (a marker or a fingerprint said "current") must not
    pass: min_rows counts the rows this run's audit rows report."""
    for source in ("ema", "openfda", "openfda_faers"):
        load = gate.load_rules(source)["load"]
        assert load["min_rows"] >= 1, source
        assert load["max_failed_inputs"] == 0, source
        assert gate.evaluate({"rows": 0, "failed_inputs": 0}, {"min_rows": load["min_rows"]})


def _ema_measures(**over):
    base = {
        "rows": 5000,
        "failed_inputs": 0,
        "peak_rss_mb": 300,
        "meta_total_equals_rows": True,
        "xref_coverage_pct": {},
    }
    base.update(over)
    return base


def test_meta_total_rule_fails_on_a_mismatch():
    rules = gate.load_rules("ema")["load"]
    assert rules["meta_total_equals_rows"] is True
    ok = _ema_measures(xref_coverage_pct={d: 100.0 for d in rules["min_xref_coverage_pct"]})
    assert gate.evaluate(ok, rules) == []
    fails = gate.evaluate({**ok, "meta_total_equals_rows": False}, rules)
    assert any("meta_total_equals_rows" in f for f in fails)
    # No measure (no feed declared a total) is a failure, not a pass.
    missing = {k: v for k, v in ok.items() if k != "meta_total_equals_rows"}
    assert any("meta_total_equals_rows" in f for f in gate.evaluate(missing, rules))
    # The rule off (False) does not check.
    assert (
        gate.evaluate({**ok, "meta_total_equals_rows": False}, {"meta_total_equals_rows": False})
        == []
    )


def test_xref_coverage_rule():
    rules = {"min_xref_coverage_pct": {"ema_medicines": 90.0, "fda_ndc": 50.0}}
    ok = {"xref_coverage_pct": {"ema_medicines": 95.5, "fda_ndc": 50.0}}
    assert gate.evaluate(ok, rules) == []
    below = gate.evaluate({"xref_coverage_pct": {"ema_medicines": 89.99, "fda_ndc": 80.0}}, rules)
    assert len(below) == 1 and "ema_medicines" in below[0] and "89.99" in below[0]
    # A dataset with no measure fails: a minimum must not read a missing value as 0 and
    # then pass a limit of 0, or read it as fine.
    gone = gate.evaluate({"xref_coverage_pct": {"fda_ndc": 80.0}}, rules)
    assert len(gone) == 1 and "ema_medicines" in gone[0] and "missing" in gone[0]
    assert gate.evaluate({}, rules) and "missing" in gate.evaluate({}, rules)[0]
    # A dataset with a limit of 0 and no measure still fails.
    assert gate.evaluate({"xref_coverage_pct": {}}, {"min_xref_coverage_pct": {"x": 0}})
    # A measure of None (the names could not be counted) fails too.
    assert gate.evaluate({"xref_coverage_pct": {"x": None}}, {"min_xref_coverage_pct": {"x": 0}})


def test_shipped_xref_rules_name_datasets_that_have_substance_fields():
    from episteme.data.regulatory.feeds import load_feeds

    feeds = load_feeds()
    for source in ("ema", "openfda"):
        limits = gate.load_rules(source)["load"]["min_xref_coverage_pct"]
        for dataset in limits:
            assert dataset in feeds and feeds[dataset].substance_fields, (source, dataset)


def test_dailymed_and_epar_extract_rules():
    dm = gate.load_rules("dailymed")["extract"]
    assert dm["allowed_licenses"] == ["public_domain"]
    assert dm["max_failed_inputs"] == 0 and dm["parts_within_caps"] is True
    epar = gate.load_rules("ema_epar")["extract"]
    assert epar["allowed_licenses"] == ["permissive"]
    # A scan of an image-only PDF is a failed input (no_text) or an empty row: both are counted.
    assert "max_empty_text_pct" in epar
    for rules in (dm, epar):
        assert rules["max_peak_rss_mb"] <= 4096
