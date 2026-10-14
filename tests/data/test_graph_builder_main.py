"""graph_builder.main CLI behaviour that needs no database (connection and build stubbed)."""

from __future__ import annotations

import contextlib
import types
from pathlib import Path

import pytest

from episteme.data import graph_builder

WRAPPER = Path(__file__).resolve().parents[2] / "scripts" / "data" / "pubmed" / "graph_pubmed.sh"


@pytest.fixture
def stub(monkeypatch):
    calls = []

    @contextlib.contextmanager
    def fake_connection():
        yield types.SimpleNamespace(commit=lambda: None)

    def fake_build(conn, **kw):
        calls.append(kw)
        if kw["source"] == "pubmed":
            return {
                "files": calls_files[0],
                "loaded": 0,
                "skipped": 0,
                "failed": [],
                "rows": {"mesh": 0},
                "facets": 0,
            }
        return {
            "source_files": [],
            "cites": 0,
            "mesh": 0,
            "missing_xml": 0,
            "skipped_no_pmid": 0,
            "parts": 0,
            "mesh_hierarchy": 0,
            "mesh_tree": 0,
            "mesh_empty_parse": 0,
        }

    calls_files = [0]
    import episteme.data.db.connection as dbc

    monkeypatch.setattr(dbc, "connection", fake_connection)
    monkeypatch.setattr(graph_builder, "build", fake_build)
    return calls, calls_files


def test_pubmed_with_zero_graph_files_warns_and_exits_1(stub, tmp_path, capsys):
    rc = graph_builder.main(["--source", "pubmed", "--processed-dir", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert f"WARNING: no pubmed graph files found under {tmp_path}" in err


def test_pubmed_with_files_exits_0(stub, tmp_path):
    _, files = stub
    files[0] = 3
    assert graph_builder.main(["--source", "pubmed", "--processed-dir", str(tmp_path)]) == 0


def test_force_note_for_non_pubmed_source(stub, tmp_path, capsys):
    rc = graph_builder.main(
        ["--source", "pmc", "--raw-dir", str(tmp_path), "--force", "--reason", "x"]
    )
    assert rc == 0
    assert "NOTE: --force has no effect for source pmc graph" in capsys.readouterr().err


def test_no_force_note_without_force(stub, tmp_path, capsys):
    graph_builder.main(["--source", "pmc", "--raw-dir", str(tmp_path)])
    assert "NOTE" not in capsys.readouterr().err


def test_graph_wrapper_leaves_directories_to_config():
    text = WRAPPER.read_text(encoding="utf-8")
    assert "--processed-dir" not in text
    assert "--raw-dir" not in text
    assert "${EPISTEME_PROCESSED_ROOT" not in text
    assert '"$@"' in text
