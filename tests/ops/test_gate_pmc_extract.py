"""The pmc extract rules pass a good extract and reject a non-commercial licence.

The rules file is run through the real measures (``gate_report.measure_staging``)
and the real evaluator, so a rule that never matches a PMC row (the licence
column holds licence codes, not subset names) fails here, not on real data.
"""

from __future__ import annotations

import json
from pathlib import Path

from episteme.data.pmc import extract_pmc as pmc
from episteme.ops import gate, gate_report

# Rules that need the run record of ``gate run`` (absent in this test).
_RECORD_RULES = ("max_peak_rss_mb", "max_failed_inputs")


def _article(raw: Path, n: int, license_code: str) -> None:
    meta = raw / "metadata" / f"PMC{n}.1.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(
        json.dumps(
            {
                "pmcid": f"PMC{n}",
                "version": 1,
                "pmid": 40_000_000 + n,
                "title": f"Article PMC{n}",
                "license_code": license_code,
            }
        ),
        encoding="utf-8",
    )
    xml = raw / "xml" / f"PMC{n}.1.xml"
    xml.parent.mkdir(parents=True, exist_ok=True)
    xml.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<article><front><article-meta>
  <article-id pub-id-type="pmc">PMC{n}</article-id>
  <title-group><article-title>Article PMC{n}</article-title></title-group>
  <abstract><p>Abstract text for PMC{n}, long enough to pass the minimum ok text
  length threshold so that the extract status of this row lands ok.</p></abstract>
</article-meta></front><body><sec><p>Body text for PMC{n}.</p></sec></body></article>""",
        encoding="utf-8",
    )


def _measure_and_evaluate(tmp_path: Path, licenses: list[str]) -> tuple[dict, list[str]]:
    raw = tmp_path / "raw"
    for i, lic in enumerate(licenses, start=1):
        _article(raw, i, lic)
    out = tmp_path / "out"
    res = pmc.extract_pmc(raw, out)
    assert res["failed"] == 0 and res["failed_articles_count"] == 0
    inputs = sorted(raw.rglob("PMC*"))
    measures = gate_report.measure_staging(out / "staging" / "pmc", inputs=inputs)
    # ``gate report`` adds the caps that wrote the parts (read from the part markers).
    measures["part_rows"], measures["part_bytes"] = pmc._part_caps()
    rules = gate.load_rules("pmc")["extract"]
    return measures, gate.evaluate(measures, rules)


def _non_record_failures(fails: list[str]) -> list[str]:
    return [f for f in fails if not any(r in f for r in _RECORD_RULES)]


def test_pmc_rules_allow_the_commercial_licence_codes(tmp_path):
    measures, fails = _measure_and_evaluate(tmp_path, ["CC BY", "CC0", "CC BY-SA", "CC BY-ND"])
    assert set(measures["license_counts"]) <= {"CC0", "CC BY", "CC BY-SA", "CC BY-ND"}
    assert _non_record_failures(fails) == []
    # Only the rules that need a run record are left (absent here).
    assert fails and all(any(r in f for r in _RECORD_RULES) for f in fails)


def test_pmc_rules_reject_a_non_commercial_licence(tmp_path):
    # The extractor does not drop CC BY-NC rows before staging (it marks them
    # subset text_mining), so the gate itself must reject them.
    measures, fails = _measure_and_evaluate(tmp_path, ["CC BY", "CC BY-NC"])
    assert "CC BY-NC" in measures["license_counts"]
    bad = [f for f in fails if f.startswith("allowed_licenses")]
    assert len(bad) == 1 and "CC BY-NC" in bad[0]


def test_pmc_rule_list_is_the_set_the_extractor_keeps():
    allowed = gate.load_rules("pmc")["extract"]["allowed_licenses"]
    assert sorted(allowed) == ["CC BY", "CC BY-ND", "CC BY-SA", "CC0"]
