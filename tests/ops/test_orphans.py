import os
import threading
import time

import pytest

from episteme import audit_trail
from episteme.ops import orphans


class FakeCursor:
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


class FakeConn:
    def __init__(self, rows):
        self.rows, self.log = rows, []

    def cursor(self):
        return FakeCursor(self.rows, self.log)


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
    conn.rollback = lambda: None
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
