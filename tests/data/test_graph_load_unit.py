"""graph_load without a database: marker discovery and the facet SQL text."""

from __future__ import annotations

from episteme.data import graph_load
from episteme.data.checkpoint_markers import mark_success


def test_graphed_files_excludes_markers_without_graph_info(tmp_path):
    mark_success(tmp_path, "pubmed", "baseline__b.xml.gz", stats={"rows": 3})
    mark_success(
        tmp_path,
        "pubmed",
        "baseline__a.xml.gz",
        stats={"graph_relations": ["mesh"], "graph_sha256": "x", "graph_rows": {"mesh": 1}},
    )
    assert graph_load.graphed_files(tmp_path, "pubmed") == ["baseline__a.xml.gz"]


def test_graphed_files_without_ops_dir_is_empty(tmp_path):
    assert graph_load.graphed_files(tmp_path, "pubmed") == []


def test_facet_sql_uses_newest_rule_version_and_drug_facet():
    assert "max(rule_version)" in graph_load.FACET_SQL
    assert "'drug'" in graph_load.FACET_SQL


def test_table_for_covers_every_relation():
    from episteme.data.graph_files import RELATIONS

    assert set(graph_load.TABLE_FOR) == set(RELATIONS)
    assert graph_load.TABLE_FOR["mesh"] == "episteme.article_mesh"


def test_truncated_marker_is_reported_corrupt_not_raised(tmp_path):
    mark_success(
        tmp_path,
        "pubmed",
        "baseline__a.xml.gz",
        stats={"graph_relations": ["mesh"], "graph_sha256": "x", "graph_rows": {"mesh": 1}},
    )
    bad = tmp_path / "_ops" / "pubmed" / "success" / "baseline__bad.xml.gz.ok"
    bad.write_text('{"stats": {"graph_rel', encoding="utf-8")
    graphed, old, corrupt = graph_load._success_markers(tmp_path, "pubmed")
    assert graphed == ["baseline__a.xml.gz"]
    assert old == []
    assert corrupt == ["baseline__bad.xml.gz"]
    assert graph_load.graphed_files(tmp_path, "pubmed") == ["baseline__a.xml.gz"]
