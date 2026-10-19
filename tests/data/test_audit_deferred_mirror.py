"""Deferred audit mirror: no mirror line for a rolled-back row."""

from __future__ import annotations

import importlib
import json

import pytest

from episteme import audit_trail


class _Cur:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *_a, **_k):
        pass

    def fetchone(self):
        return None


class _Conn:
    def __init__(self, fail_commit: bool = False):
        self.commits = 0
        self.rollbacks = 0
        self.fail_commit = fail_commit

    def cursor(self):
        return _Cur()

    def commit(self):
        if self.fail_commit:
            raise OSError("commit failed")
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    d = tmp_path / "_audit"
    monkeypatch.setattr(audit_trail, "_mirror_dir", lambda: d)
    monkeypatch.setattr(audit_trail, "require_actor", lambda: "tester")
    monkeypatch.setattr(audit_trail, "_code_version", lambda: "abc")

    def lines():
        return [
            line
            for f in sorted(d.glob("audit-*.jsonl"))
            for line in f.read_text(encoding="utf-8").splitlines()
        ]

    return lines


def test_record_in_deferred_mirror_queues_until_commit(mirror):
    conn = _Conn()
    with audit_trail.deferred_mirror(conn):
        h = audit_trail.record("run_start", conn=conn, object="a")
        assert mirror() == []
        audit_trail.commit(conn)
    assert conn.commits == 1
    lines = mirror()
    assert len(lines) == 1
    assert json.loads(lines[0])["record_hash"] == h


def test_commit_appends_in_order(mirror):
    conn = _Conn()
    with audit_trail.deferred_mirror(conn):
        audit_trail.record("run_start", conn=conn, object="first")
        audit_trail.record("run_end", conn=conn, object="second")
        audit_trail.commit(conn)
    assert [json.loads(x)["object"] for x in mirror()] == ["first", "second"]


def test_rollback_drops_queue(mirror):
    conn = _Conn()
    with audit_trail.deferred_mirror(conn):
        audit_trail.record("run_start", conn=conn, object="a")
        audit_trail.rollback(conn)
        assert conn.rollbacks == 1
        assert mirror() == []
        audit_trail.commit(conn)  # nothing left to append
    assert mirror() == []


def test_leaving_with_queued_lines_raises_and_drops(mirror):
    conn = _Conn()
    with (
        pytest.raises(
            RuntimeError,
            match="deferred audit mirror lines were neither committed nor rolled back",
        ),
        audit_trail.deferred_mirror(conn),
    ):
        audit_trail.record("run_start", conn=conn, object="a")
    assert mirror() == []
    # queue is gone: a later record on the same conn is not deferred
    audit_trail.record("run_start", conn=conn, object="b")
    assert len(mirror()) == 1


def test_exception_exit_drops_queue_and_propagates(mirror):
    conn = _Conn()
    with pytest.raises(ValueError, match="boom"), audit_trail.deferred_mirror(conn):
        audit_trail.record("run_start", conn=conn, object="a")
        raise ValueError("boom")
    assert mirror() == []


def test_nesting_is_an_error():
    conn = _Conn()
    with audit_trail.deferred_mirror(conn), pytest.raises(RuntimeError, match="nested"):
        with audit_trail.deferred_mirror(conn):
            pass


def test_commit_failure_drops_queue_and_reraises(mirror):
    conn = _Conn(fail_commit=True)
    with audit_trail.deferred_mirror(conn):
        audit_trail.record("run_start", conn=conn, object="a")
        with pytest.raises(OSError, match="commit failed"):
            audit_trail.commit(conn)
    assert mirror() == []


def test_outside_deferred_mirror_unchanged(mirror):
    conn = _Conn()
    audit_trail.record("run_start", conn=conn, object="a")
    assert len(mirror()) == 1
    audit_trail.commit(conn)
    audit_trail.rollback(conn)
    assert len(mirror()) == 1


@pytest.mark.pg
def test_pg_rollback_then_commit_verifies(pg_conn, monkeypatch, tmp_path):
    monkeypatch.setenv("EPISTEME_ACTOR", "test-actor")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    import episteme.config as cfg

    importlib.reload(cfg)
    cfg.get_settings.cache_clear()
    at = importlib.reload(audit_trail)

    with (
        pg_conn.cursor() as cur,
        open("src/episteme/data/db/extensions.sql") as ext,
        open("src/episteme/data/db/schema.sql") as sch,
    ):
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute(ext.read())
        cur.execute(sch.read())
    pg_conn.commit()

    with at.deferred_mirror(pg_conn):
        at.record("run_start", conn=pg_conn, object="x")
        at.record("run_end", conn=pg_conn, object="y")
        at.rollback(pg_conn)
        h = at.record("run_start", conn=pg_conn, object="z")
        at.commit(pg_conn)

    assert at.verify(pg_conn) == []
    files = list((tmp_path / "_ops" / "_audit").glob("audit-*.jsonl"))
    lines = [ln for f in files for ln in f.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    with pg_conn.cursor() as cur:
        cur.execute("SELECT record_hash FROM episteme._audit")
        rows = cur.fetchall()
    assert [r[0] for r in rows] == [h] == [json.loads(lines[0])["record_hash"]]
