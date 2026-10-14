"""PubMed data-quality audit over shards and graph files. No DB."""

from __future__ import annotations

import json

from episteme.data.audit.pubmed_audit import orcid_check_ok, run_audit
from episteme.data.pubmed.extract_pubmed import extract_pubmed

from .test_extract_pubmed import _pubmed_xml_gz


def test_orcid_check_digit():
    assert orcid_check_ok("0000-0002-1825-0097")
    assert not orcid_check_ok("0000-0002-1825-0098")
    assert orcid_check_ok("0000-0002-1694-233X")


def test_run_audit_on_fixture_tree(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    for sub, pmid in (("baseline", "1"), ("updates", "2")):
        _pubmed_xml_gz(raw / sub / "pubmed26n0001.xml.gz", pmid=pmid, title="T", token=sub)
    assert extract_pubmed(raw, out, workers=1)["ok"] == 2

    report = run_audit(
        out, raw_dir=raw, out_dir=tmp_path / "audit", sample_files=2, duckdb_temp=tmp_path / "tmp"
    )

    assert (tmp_path / "audit" / "pubmed_audit.json").is_file()
    assert (tmp_path / "audit" / "pubmed_audit.md").is_file()
    assert json.loads((tmp_path / "audit" / "pubmed_audit.json").read_text())["rows"] == 2
    assert report["rows"] == 2
    assert report["doi_probe"] == {"files": 2, "records": 2, "mismatch": 0}
    assert "mesh" in report["graph_rows"]
    assert report["mesh_ui_known_pct"] is None
    assert report["id_mismatch"] == 0
    assert report["dup_ids_across_shards"] == 0
