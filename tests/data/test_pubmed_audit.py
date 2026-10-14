"""PubMed data-quality audit over shards and graph files. No DB."""

from __future__ import annotations

import json

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from episteme.data.audit import pubmed_audit
from episteme.data.audit.pubmed_audit import orcid_check_ok, run_audit
from episteme.data.graph_files import graph_file_path, graph_schema
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
    assert report["doi_probe"] == {"files": 2, "records": 2, "mismatch": 0, "status": "ok"}
    assert "mesh" in report["graph_rows"]
    assert report["mesh_ui_known_pct"] is None
    assert report["id_mismatch"] == 0
    assert report["dup_ids_across_shards"] == 0


def _tree(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    for sub, pmid in (("baseline", "1"), ("updates", "2")):
        _pubmed_xml_gz(raw / sub / "pubmed26n0001.xml.gz", pmid=pmid, title="T", token=sub)
    assert extract_pubmed(raw, out, workers=1)["ok"] == 2
    return raw, out


def _write_rel(out, rel, key, rows):
    cols = graph_schema(rel).names
    table = pa.table({c: [r.get(c) for r in rows] for c in cols}, schema=graph_schema(rel))
    pq.write_table(table, graph_file_path(out, "pubmed", rel, key))


def test_populated_graph_checks(tmp_path):
    raw, out = _tree(tmp_path)
    key = "baseline__pubmed26n0001.xml.gz"  # populated; the updates file stays empty
    shard = out / "staging" / "pubmed" / "baseline__pubmed26n0001.parquet"
    df = pl.read_parquet(shard).with_columns(is_retracted=pl.lit(True))
    df.write_parquet(shard)

    _write_rel(
        out,
        "mesh",
        key,
        [{"pmid": "1", "descriptor_ui": "D001"}, {"pmid": "1", "descriptor_ui": "D999"}],
    )
    _write_rel(
        out,
        "cites",
        key,
        [{"src_pmid": "1", "dst_pmid": "2"}, {"src_pmid": "1", "dst_pmid": "999"}],
    )
    _write_rel(
        out,
        "authors",
        key,
        [
            {"pmid": "1", "position": 1, "orcid": "0000-0002-1825-0097"},
            {"pmid": "1", "position": 2, "orcid": "0000-0002-1825-0098"},
        ],
    )
    _write_rel(out, "comments", key, [{"pmid": "2", "ref_type": "RetractionIn"}])
    mesh_dir = out / "staging" / "mesh"
    mesh_dir.mkdir(parents=True)
    pq.write_table(pa.table({"id": ["mesh:D001"]}), mesh_dir / "mesh.parquet")

    r = run_audit(out, raw_dir=raw, out_dir=tmp_path / "audit", sample_files=2)

    assert r["graph_rows"]["mesh"] == 2 and r["graph_rows"]["cites"] == 2
    assert r["mesh_ui_known_pct"] == 50.0
    assert r["cites_resolved_pct"] == 50.0
    assert r["orcid_checksum_bad"] == 1
    assert r["retraction_in_without_flag"] == 1  # pmid 2 has a link, but is not flagged
    assert r["retracted_without_link"] == 1  # pmid 1 is flagged, no link
    assert r["doi_probe"]["status"] == "ok"


def test_doi_probe_status_skipped_and_no_records(tmp_path):
    raw, out = _tree(tmp_path)
    r = run_audit(out, raw_dir=None, out_dir=tmp_path / "a1", sample_files=2)
    assert r["doi_probe"]["status"] == "skipped_no_raw_dir"
    r = run_audit(out, raw_dir=tmp_path / "missing", out_dir=tmp_path / "a2", sample_files=2)
    assert r["doi_probe"]["status"] == "skipped_no_raw_dir"
    # raw files whose records have no stored rows -> nothing compared
    other = tmp_path / "raw2"
    _pubmed_xml_gz(other / "pubmed26n0009.xml.gz", pmid="77", title="T", token="x")
    r = run_audit(out, raw_dir=other, out_dir=tmp_path / "a3", sample_files=2)
    assert r["doi_probe"]["status"] == "no_records"


def test_main_exit_codes(tmp_path, capsys):
    raw, out = _tree(tmp_path)
    base = ["--processed-dir", str(out), "--out-dir", str(tmp_path / "a")]
    assert pubmed_audit.main([*base, "--raw-dir", str(raw), "--sample-files", "2"]) == 0
    assert pubmed_audit.main([*base, "--raw-dir", str(tmp_path / "nope")]) == 1
    assert "WARNING" in capsys.readouterr().out
    assert (
        pubmed_audit.main([*base, "--raw-dir", str(tmp_path / "nope"), "--sample-files", "0"]) == 0
    )
