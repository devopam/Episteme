"""graph_load without a database: marker discovery and the facet SQL text."""

from __future__ import annotations

import types

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


def test_stop_survives_a_failing_rollback_and_is_not_retried(monkeypatch, tmp_path):
    """A KeyboardInterrupt in a unit stays a KeyboardInterrupt even when the rollback
    raises OperationalError (lost connection); with_retries must not retry it."""
    import contextlib

    import psycopg
    import pytest

    import episteme.data.db.connection as dbc

    class Conn:
        def __init__(self, bad_rollback=True):
            self.bad_rollback = bad_rollback

        def cursor(self):
            return contextlib.nullcontext(
                types.SimpleNamespace(execute=lambda *a, **k: None, fetchone=lambda: (1,))
            )

        def rollback(self):
            if not self.bad_rollback:
                return
            raise psycopg.OperationalError("connection lost")

        def cancel_safe(self):
            pass

    @contextlib.contextmanager
    def fake_connection():
        yield Conn()

    sleeps = []
    monkeypatch.setattr(dbc, "connection", fake_connection)
    monkeypatch.setattr(graph_load, "_SLEEP", sleeps.append)
    monkeypatch.setattr(graph_load, "_success_markers", lambda d, s: (["a.xml.gz"], [], []))

    def stop(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(graph_load, "marker_graph_info", stop)
    with pytest.raises(KeyboardInterrupt):
        graph_load.build_pubmed_graph(Conn(False), processed_dir=tmp_path, run_id="r")
    assert sleeps == []
