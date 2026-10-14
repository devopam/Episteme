"""graph_files: fixed per-relation schemas, a streaming writer that publishes all
relation files atomically (empty ones too), and marker read-back. No DB."""

from __future__ import annotations

import json

import pyarrow.parquet as pq
import pytest

from episteme.data import graph_files as gf
from episteme.data.checkpoint_markers import mark_success


def _mesh(pmid: str) -> dict:
    return {
        "pmid": pmid,
        "descriptor_ui": "D006801",
        "descriptor_name": "Humans",
        "major_topic": False,
        "qualifier_uis": [],
        "qualifiers": [],
        "source_file": "baseline__pubmed26n0001.xml.gz",
    }


def test_relations_and_schemas_are_fixed():
    assert gf.RELATIONS == ("mesh", "cites", "comments", "chemicals", "keywords", "authors")
    s = gf.graph_schema("authors")
    assert s.names == list(gf.RELATION_COLUMNS["authors"])
    assert str(s.field("position").type) == "int32"
    assert str(s.field("affiliations").type) == "list<item: string>"
    assert str(gf.graph_schema("mesh").field("major_topic").type) == "bool"


def test_graph_file_path_uses_shard_stem(tmp_path):
    p = gf.graph_file_path(tmp_path, "pubmed", "mesh", "baseline__pubmed26n0001.xml.gz")
    assert p == tmp_path / "staging" / "pubmed_graph" / "mesh" / "baseline__pubmed26n0001.parquet"


def test_writer_publishes_every_relation_including_empty(tmp_path):
    sf = "baseline__pubmed26n0001.xml.gz"
    with gf.GraphFileWriter(tmp_path, "pubmed", sf, batch_rows=2) as w:
        w.add("mesh", [_mesh("1"), _mesh("2"), _mesh("3")])
        info = w.close()
    assert info["graph_relations"] == list(gf.RELATIONS)
    assert info["graph_rows"]["mesh"] == 3
    assert info["graph_rows"]["cites"] == 0
    for rel in gf.RELATIONS:
        path = gf.graph_file_path(tmp_path, "pubmed", rel, sf)
        assert path.is_file()
        assert pq.read_schema(path).names == list(gf.RELATION_COLUMNS[rel])
    assert pq.read_table(gf.graph_file_path(tmp_path, "pubmed", "mesh", sf)).num_rows == 3
    assert not list(tmp_path.rglob("*.tmp"))
    assert len(info["graph_sha256"]) == 64


def test_writer_aborts_on_exception_and_leaves_nothing(tmp_path):
    sf = "baseline__pubmed26n0002.xml.gz"
    with pytest.raises(RuntimeError):
        with gf.GraphFileWriter(tmp_path, "pubmed", sf, batch_rows=1) as w:
            w.add("mesh", [_mesh("1"), _mesh("2")])
            raise RuntimeError("boom")
    assert not list(tmp_path.rglob("*.parquet"))
    assert not list(tmp_path.rglob("*.tmp"))


def test_unknown_relation_is_rejected(tmp_path):
    with gf.GraphFileWriter(tmp_path, "pubmed", "x.xml.gz") as w:
        with pytest.raises(KeyError):
            w.add("grants", [{}])
        w.abort()


def test_marker_graph_info_round_trip(tmp_path):
    sf = "baseline__pubmed26n0003.xml.gz"
    assert gf.marker_graph_info(tmp_path, "pubmed", sf) is None
    mark_success(tmp_path, "pubmed", sf, stats={"n_rows": 1})
    assert gf.marker_graph_info(tmp_path, "pubmed", sf) is None  # no graph keys: old marker
    stats = {"graph_relations": list(gf.RELATIONS), "graph_sha256": "a" * 64, "graph_rows": {}}
    mark_success(tmp_path, "pubmed", sf, stats=stats)
    got = gf.marker_graph_info(tmp_path, "pubmed", sf)
    assert got["graph_sha256"] == "a" * 64
    assert json.loads(json.dumps(got)) == got


def test_sha256_changes_with_content(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    assert gf.graph_sha256([a, b]) != gf.graph_sha256([b, a])
