"""extract_pubmed writes graph files with the articles shard; marker last. No DB."""

from __future__ import annotations

import pyarrow.parquet as pq
import pytest

from episteme.data import graph_files as gf
from episteme.data.checkpoint_markers import is_success
from episteme.data.pubmed import extract_pubmed as ep

from .test_extract_pubmed import _pubmed_xml_gz


def _raw(tmp_path):
    raw = tmp_path / "raw"
    (raw / "baseline").mkdir(parents=True)
    _pubmed_xml_gz(raw / "baseline" / "pubmed26n0001.xml.gz", pmid="1", title="T", token="a")
    return raw


def test_graph_files_and_marker(tmp_path):
    raw, out = _raw(tmp_path), tmp_path / "out"
    res = ep.extract_pubmed(raw, out, workers=1)
    assert res["ok"] == 1
    key = "baseline__pubmed26n0001.xml.gz"
    info = gf.marker_graph_info(out, "pubmed", key)
    assert info["graph_relations"] == list(gf.RELATIONS)
    for rel in gf.RELATIONS:
        assert gf.graph_file_path(out, "pubmed", rel, key).is_file()


def test_crash_before_marker_redoes_file(tmp_path, monkeypatch):
    raw, out = _raw(tmp_path), tmp_path / "out"
    key = "baseline__pubmed26n0001.xml.gz"

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ep, "write_rows", boom)
    ep.extract_pubmed(raw, out, workers=1)
    assert not is_success(out, "pubmed", key)
    assert not list((out / "staging").rglob("*.tmp"))
    monkeypatch.undo()
    assert ep.extract_pubmed(raw, out, workers=1)["ok"] == 1
    assert gf.marker_graph_info(out, "pubmed", key) is not None


def test_same_basename_in_two_dirs_never_collides(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    for sub, pmid in (("baseline", "1"), ("updates", "2")):
        (raw / sub).mkdir(parents=True)
        _pubmed_xml_gz(raw / sub / "pubmed26n0001.xml.gz", pmid=pmid, title="T", token=sub)
    ep.extract_pubmed(raw, out, workers=1)
    a = gf.graph_file_path(out, "pubmed", "authors", "baseline__pubmed26n0001.xml.gz")
    b = gf.graph_file_path(out, "pubmed", "authors", "updates__pubmed26n0001.xml.gz")
    assert a != b and a.is_file() and b.is_file()


@pytest.mark.parametrize("rel", gf.RELATIONS)
def test_one_and_three_workers_write_identical_graph_files(tmp_path, rel):
    raw = tmp_path / "raw"
    (raw / "baseline").mkdir(parents=True)
    for i in range(1, 5):
        _pubmed_xml_gz(
            raw / "baseline" / f"pubmed26n000{i}.xml.gz", pmid=str(i), title="T", token=str(i)
        )
    ep.extract_pubmed(raw, tmp_path / "w1", workers=1)
    ep.extract_pubmed(raw, tmp_path / "w3", workers=3)
    for i in range(1, 5):
        key = f"baseline__pubmed26n000{i}.xml.gz"
        t1 = pq.read_table(gf.graph_file_path(tmp_path / "w1", "pubmed", rel, key))
        t3 = pq.read_table(gf.graph_file_path(tmp_path / "w3", "pubmed", rel, key))
        assert t1.equals(t3)
