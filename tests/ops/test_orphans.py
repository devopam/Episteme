import os
import threading
import time

import pytest

from episteme import audit_trail
from episteme.ops import orphans


class FakeCursor:
    cancel_result = True

    def __init__(self, rows, log):
        self.rows, self.log = rows, log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return (self.cancel_result,)


class FakeConn:
    def __init__(self, rows):
        self.rows, self.log = rows, []

    def cursor(self):
        return FakeCursor(self.rows, self.log)

    def rollback(self):
        self.log.append(("ROLLBACK", None))


def test_sweep_cancels_only_dead_clients(monkeypatch):
    rows = [
        (101, "episteme:r1:5000", "2026-10-19 10:00:00"),
        (102, "episteme:r1:5001", "2026-10-19 10:01:00"),
        (103, "episteme:garbage", "2026-10-19 10:02:00"),
        (104, "episteme:r1:notanumber", "2026-10-19 10:03:00"),
    ]
    conn = FakeConn(rows)
    monkeypatch.setattr(orphans.psutil, "pid_exists", lambda p: p == 5000)
    recorded = []
    monkeypatch.setattr(
        orphans.audit_trail, "record", lambda ev, **kw: recorded.append((ev, kw)) or "h"
    )
    commits = []
    monkeypatch.setattr(orphans.audit_trail, "commit", lambda c: commits.append(c))
    assert orphans.sweep(conn) == [102]
    cancels = [p for s, p in conn.log if "pg_cancel_backend" in s]
    assert cancels == [(102,)]
    assert len(recorded) == 1
    ev, kw = recorded[0]
    assert ev == "orphan_cancel"
    assert kw["object"] == "episteme:r1:5001"
    assert "2026-10-19 10:01:00" in kw["reason"]
    assert len(commits) == 1


def test_sweep_no_orphans_records_nothing(monkeypatch):
    conn = FakeConn([(1, "episteme:r:5000", "t")])
    monkeypatch.setattr(orphans.psutil, "pid_exists", lambda p: True)
    monkeypatch.setattr(orphans.audit_trail, "record", lambda *a, **k: pytest.fail("no"))
    assert orphans.sweep(conn) == []


def test_orphan_cancel_is_a_known_event_type():
    assert "orphan_cancel" in audit_trail.EVENT_TYPES


def _dead_pid() -> int:
    import psutil

    p = 4_000_000
    while psutil.pid_exists(p):
        p += 1
    return p


@pytest.mark.pg
def test_sweep_cancels_backend_of_dead_client(pg_conn):
    import psycopg

    dsn = os.environ["TEST_PG_DSN"]
    dead = _dead_pid()
    name = f"episteme:test-orphan-{time.time_ns() % 10**6}:{dead}"
    victim = psycopg.connect(dsn, application_name=name, autocommit=True)
    out = {}

    def run():
        try:
            victim.execute("SELECT pg_sleep(60)")
        except psycopg.errors.QueryCanceled as e:
            out["err"] = e

    th = threading.Thread(target=run)
    th.start()
    time.sleep(1.5)
    victim_pid = victim.info.backend_pid
    try:
        cancelled = orphans.sweep(pg_conn, name_like=name)
        th.join(10)
    finally:
        victim.close()
    assert victim_pid in cancelled
    assert isinstance(out.get("err"), psycopg.errors.QueryCanceled)


def test_sweep_filters_active_only():
    assert "state = 'active'" in orphans._SELECT


def test_sweep_skips_backend_already_ended(monkeypatch):
    conn = FakeConn([(7, "episteme:r:5001", "t")])
    monkeypatch.setattr(orphans.psutil, "pid_exists", lambda p: False)
    monkeypatch.setattr(FakeCursor, "cancel_result", False)
    monkeypatch.setattr(orphans.audit_trail, "record", lambda *a, **k: pytest.fail("no"))
    assert orphans.sweep(conn) == []


def test_sweep_audit_failure_continues_then_raises(monkeypatch):
    rows = [(11, "episteme:r:6001", "t1"), (12, "episteme:r:6002", "t2")]
    conn = FakeConn(rows)
    monkeypatch.setattr(orphans.psutil, "pid_exists", lambda p: False)
    recorded = []

    def rec(ev, **kw):
        if kw["object"].endswith("6001"):
            raise ValueError("audit down")
        recorded.append(kw["object"])

    monkeypatch.setattr(orphans.audit_trail, "record", rec)
    commits = []
    monkeypatch.setattr(orphans.audit_trail, "commit", lambda c: commits.append(1))
    with pytest.raises(RuntimeError, match=r"\[11\]"):
        orphans.sweep(conn)
    cancels = [p for s, p in conn.log if s != "ROLLBACK" and "pg_cancel_backend" in s]
    assert cancels == [(11,), (12,)]
    assert recorded == ["episteme:r:6002"]
    assert commits == [1]
    assert ("ROLLBACK", None) in conn.log


def test_sweep_failed_commit_leaves_no_mirror_line(monkeypatch):
    """record() queues its mirror line inside deferred_mirror; the commit fails,
    so the line is dropped, the sweep goes on and raises after the last orphan."""
    rows = [(21, "episteme:r:7001", "t1"), (22, "episteme:r:7002", "t2")]

    class CommitConn(FakeConn):
        failed_once = False

        def commit(self):  # the first commit (backend 21) fails
            if not self.failed_once:
                self.failed_once = True
                raise OSError("commit failed")
            self.log.append(("COMMIT", None))

    conn = CommitConn(rows)
    monkeypatch.setattr(orphans.psutil, "pid_exists", lambda p: False)

    def rec(ev, **kw):
        audit_trail._DEFERRED[kw["conn"]].append({"object": kw["object"]})
        return "h"

    monkeypatch.setattr(orphans.audit_trail, "record", rec)
    written = []
    monkeypatch.setattr(audit_trail, "_append_mirror_lines", lambda lines: written.extend(lines))
    with pytest.raises(RuntimeError, match=r"\[21\]"):
        orphans.sweep(conn)
    assert written == [{"object": "episteme:r:7002"}]  # none for the failed commit
    assert conn.log.count(("COMMIT", None)) == 1
    assert ("ROLLBACK", None) in conn.log
    assert conn not in audit_trail._DEFERRED


def test_sweep_records_inside_deferred_mirror(monkeypatch):
    conn = FakeConn([(31, "episteme:r:8001", "t")])
    monkeypatch.setattr(orphans.psutil, "pid_exists", lambda p: False)
    seen = []
    monkeypatch.setattr(
        orphans.audit_trail,
        "record",
        lambda ev, **kw: seen.append(kw["conn"] in audit_trail._DEFERRED),
    )
    monkeypatch.setattr(orphans.audit_trail, "commit", lambda c: None)
    assert orphans.sweep(conn) == [31]
    assert seen == [True]
