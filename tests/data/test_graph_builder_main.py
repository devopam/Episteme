"""graph_builder.main CLI behaviour that needs no database (connection and build stubbed)."""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from episteme.data import graph_builder

WRAPPER = Path(__file__).resolve().parents[2] / "scripts" / "data" / "pubmed" / "graph_pubmed.sh"


class _FakeConn:
    """A connection stand-in (weak-referenceable, as audit_trail.deferred_mirror needs)."""

    def commit(self):
        pass

    def rollback(self):
        pass

    def cancel_safe(self):
        pass


@pytest.fixture
def stub(monkeypatch):
    calls = []

    @contextlib.contextmanager
    def fake_connection():
        yield _FakeConn()

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
    monkeypatch.setattr(graph_builder.orphans, "sweep", lambda conn: [])
    monkeypatch.setattr(graph_builder, "stage_guard", lambda ref: contextlib.nullcontext())
    monkeypatch.setattr(graph_builder, "_SLEEP", lambda s: None)
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


# --- fail-fast, retry and failure record (operating standard PR 2, Task 8) ---


@pytest.fixture
def recorded(monkeypatch):
    recs = []
    monkeypatch.setattr(
        graph_builder, "record_failure", lambda *a, **k: recs.append((a, k)) or Path("x")
    )
    return recs


def test_pubmed_failure_is_recorded_with_the_stopped_file_and_exits_1(
    stub, recorded, tmp_path, monkeypatch, capsys
):
    def failing_build(conn, **kw):
        return {
            "files": 3,
            "loaded": 1,
            "skipped": 0,
            "failed": ["b.xml.gz"],
            "stopped_at": "b.xml.gz",
            "error": "GraphLoadError('b.xml.gz: boom')",
            "rows": {"mesh": 0},
            "facets": 0,
        }

    monkeypatch.setattr(graph_builder, "build", failing_build)
    assert graph_builder.main(["--source", "pubmed", "--processed-dir", str(tmp_path)]) == 1
    assert len(recorded) == 1
    args, _ = recorded[0]
    assert args[:3] == ("pubmed", "graph", "b.xml.gz")
    assert "boom" in args[3]
    assert "stopped_at=b.xml.gz" in capsys.readouterr().err


def test_other_source_transient_error_retries_on_a_fresh_connection(
    stub, recorded, tmp_path, monkeypatch, capsys
):
    import psycopg

    sleeps = []
    monkeypatch.setattr(graph_builder, "_SLEEP", sleeps.append)
    attempts = []
    real = graph_builder.build

    def flaky(conn, **kw):
        attempts.append(conn)
        if len(attempts) == 1:
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        return real(conn, **kw)

    monkeypatch.setattr(graph_builder, "build", flaky)
    rc = graph_builder.main(["--source", "pmc", "--raw-dir", str(tmp_path)])
    assert rc == 0
    assert len(attempts) == 2 and attempts[0] is not attempts[1]
    assert len(sleeps) == 1
    assert recorded == []


def test_other_source_permanent_failure_records_the_source_and_exits_1(
    stub, recorded, tmp_path, monkeypatch, capsys
):
    def boom(conn, **kw):
        raise ValueError("bad row")

    monkeypatch.setattr(graph_builder, "build", boom)
    assert graph_builder.main(["--source", "pmc", "--raw-dir", str(tmp_path)]) == 1
    assert len(recorded) == 1
    args, _ = recorded[0]
    assert args[:3] == ("pmc", "graph", "pmc")
    assert "bad row" in args[3]


def test_main_sweeps_orphans_once_at_start(stub, tmp_path, monkeypatch):
    sweeps = []
    monkeypatch.setattr(graph_builder.orphans, "sweep", lambda conn: sweeps.append(conn) or [])
    graph_builder.main(["--source", "pmc", "--raw-dir", str(tmp_path)])
    assert len(sweeps) == 1


def test_holder_reaches_stage_guard_conn_ref(stub, tmp_path, monkeypatch):
    calls, files = stub
    files[0] = 1
    refs = []

    @contextlib.contextmanager
    def capture(ref):
        refs.append(ref)
        yield

    monkeypatch.setattr(graph_builder, "stage_guard", capture)
    assert graph_builder.main(["--source", "pubmed", "--processed-dir", str(tmp_path)]) == 0
    holder = calls[0]["holder"]
    sentinel = object()
    holder[0] = sentinel
    assert refs[0]() is sentinel  # the list build() fills is the one stage_guard reads


def test_pubmed_unexpected_error_is_recorded_and_exits_1(
    stub, recorded, tmp_path, monkeypatch, capsys
):
    import psycopg

    def boom(conn, **kw):
        raise psycopg.OperationalError("db gone")

    monkeypatch.setattr(graph_builder, "build", boom)
    assert graph_builder.main(["--source", "pubmed", "--processed-dir", str(tmp_path)]) == 1
    assert recorded[0][0][:3] == ("pubmed", "graph", "pubmed")


def test_pubmed_stop_propagates_to_stage_guard(stub, tmp_path, monkeypatch):
    def stop(conn, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(graph_builder, "build", stop)
    with pytest.raises(KeyboardInterrupt):
        graph_builder.main(["--source", "pubmed", "--processed-dir", str(tmp_path)])
