"""The stage lock and the advisory lock (ops/lock.py), wired into stage_guard."""

from __future__ import annotations

import contextlib
import json
import os

import pytest

from episteme.config import get_settings
from episteme.ops import lock, stop


def _env(monkeypatch, run_id=None, runner_pid=None):
    for key, val in (("EPISTEME_RUN_ID", run_id), ("EPISTEME_RUNNER_PID", runner_pid)):
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(val))
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_settings():
    yield
    get_settings.cache_clear()


def _heavy():
    lock.lock_dir().mkdir(parents=True, exist_ok=True)
    return lock.lock_dir() / "heavy.lock"


def _rec(**fields):
    rec = {
        "kind": "heavy",
        "pid": os.getpid(),
        "create_time": lock.create_time(os.getpid()),
        "run_id": "r1",
        "stage": "s",
        "started_at": "2026-10-20T00:00:00+00:00",
        "state": "running",
    }
    rec.update(fields)
    return rec


def test_stage_lock_without_run_id_uses_this_pid(monkeypatch):
    _env(monkeypatch)
    with lock.stage_lock("load") as run_id:
        assert run_id == f"adhoc-{os.getpid()}"
        rec = json.loads(_heavy().read_text(encoding="utf-8"))
        assert rec["pid"] == os.getpid()
        assert rec["run_id"] == run_id
        assert rec["stage"] == "load"
    assert not _heavy().exists()


def test_stage_lock_with_run_id_is_reentrant(monkeypatch):
    _env(monkeypatch, run_id="r1", runner_pid=os.getpid())
    _heavy().write_text(json.dumps(_rec(run_id="r1")), encoding="utf-8")
    with lock.stage_lock("load") as run_id:
        assert run_id == "r1"
    assert _heavy().exists()  # the outer owner still holds it


def test_stage_lock_child_of_a_directly_run_wrapper_reenters(monkeypatch):
    """The bash wrapper holds the lock as adhoc-<runner pid> with owner <runner pid>;
    its Python child has no run id and must re-enter, not wait."""
    _env(monkeypatch, runner_pid=os.getpid())
    _heavy().write_text(json.dumps(_rec(run_id=f"adhoc-{os.getpid()}")), encoding="utf-8")
    waits = []
    monkeypatch.setattr(lock.time, "sleep", lambda s: waits.append(s))
    with lock.stage_lock("extract") as run_id:
        assert run_id == f"adhoc-{os.getpid()}"
    assert waits == []
    assert _heavy().exists()


def test_stage_guard_waits_for_another_live_run(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(stop, "install_stop_handlers", lambda: None)
    monkeypatch.setattr(stop, "start_parent_watchdog", lambda *a, **k: None)
    _heavy().write_text(json.dumps(_rec(run_id="other")), encoding="utf-8")
    sleeps = []

    def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) == 2:
            _heavy().unlink()

    monkeypatch.setattr(lock.time, "sleep", fake_sleep)
    ran = []
    with stop.stage_guard(lambda: None, lock_stage="load"):
        ran.append(json.loads(_heavy().read_text(encoding="utf-8"))["run_id"])
    assert ran == [f"adhoc-{os.getpid()}"]
    assert len(sleeps) >= 2
    assert not _heavy().exists()


def test_stage_guard_without_lock_stage_takes_no_lock(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(stop, "install_stop_handlers", lambda: None)
    monkeypatch.setattr(stop, "start_parent_watchdog", lambda *a, **k: None)
    with stop.stage_guard(lambda: None):
        assert not _heavy().exists()


def test_stage_lock_db_enters_and_leaves_the_advisory_lock(monkeypatch):
    _env(monkeypatch)
    events = []

    @contextlib.contextmanager
    def fake_advisory(**kw):
        events.append("enter")
        assert _heavy().exists()  # the file lock comes first
        yield
        events.append("exit")

    monkeypatch.setattr(lock, "advisory_lock", fake_advisory)
    with lock.stage_lock("load", db=True):
        assert events == ["enter"]
    assert events == ["enter", "exit"]
    with lock.stage_lock("load"):
        pass
    assert events == ["enter", "exit"]


class _Stop(Exception):
    pass


def _guard_kwargs(monkeypatch, module, argv=()):
    """Run ``module.main`` with a recording stage_guard that stops the run."""
    got = {}

    def fake_guard(ref, **kw):
        got.update(kw)
        raise _Stop

    monkeypatch.setattr(module, "stage_guard", fake_guard)
    with pytest.raises(_Stop):
        module.main(list(argv))
    return got


def test_extractor_main_passes_lock_stage(monkeypatch):
    from episteme.data.pubmed import extract_pubmed

    assert _guard_kwargs(monkeypatch, extract_pubmed) == {
        "lock_stage": "extract",
        "argv": [],
        "source": "pubmed",
    }


def test_serializer_main_passes_lock_stage(monkeypatch):
    from episteme.data.mesh import serialize_mesh

    assert _guard_kwargs(monkeypatch, serialize_mesh) == {
        "lock_stage": "serialize",
        "argv": [],
        "source": "mesh",
    }


def test_stop_during_the_file_lock_wait_exits_130_and_leaves_no_files(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(stop, "install_stop_handlers", lambda: None)
    monkeypatch.setattr(stop, "start_parent_watchdog", lambda *a, **k: None)
    heavy = _heavy()
    heavy.write_text(json.dumps(_rec(run_id="other")), encoding="utf-8")

    def interrupt(sec):
        raise KeyboardInterrupt

    monkeypatch.setattr(lock.time, "sleep", interrupt)
    with pytest.raises(SystemExit) as exc:
        with stop.stage_guard(lambda: None, lock_stage="load"):
            pytest.fail("the body must not run while another run holds the lock")
    assert exc.value.code == 130
    assert json.loads(heavy.read_text(encoding="utf-8"))["run_id"] == "other"  # not ours
    assert not (heavy.parent / "heavy.request").exists()


class _FakeConn:
    def __init__(self, try_result=True):
        self.calls = []
        self.closed = False
        self.try_result = try_result

    def execute(self, sql, params=None):
        self.calls.append(sql)
        outer = self

        class _Cur:
            def fetchone(self):
                return (outer.try_result,)

        return _Cur()

    def close(self):
        self.closed = True


def _fake_connect(monkeypatch, conn):
    import psycopg

    from episteme.data.db import connection as dbc

    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: conn)
    monkeypatch.setattr(dbc, "dsn_from_settings", lambda: "dbname=x")


def test_advisory_lock_unlocks_and_closes_when_the_body_raises(monkeypatch):
    conn = _FakeConn()
    _fake_connect(monkeypatch, conn)
    with pytest.raises(RuntimeError, match="boom"):
        with lock.advisory_lock():
            raise RuntimeError("boom")
    assert any("pg_advisory_unlock" in c for c in conn.calls)
    assert conn.closed


def test_advisory_lock_busy_closes_without_unlock(monkeypatch):
    conn = _FakeConn(try_result=False)
    _fake_connect(monkeypatch, conn)
    with pytest.raises(lock.LockBusy):
        with lock.advisory_lock(wait=False):
            pass
    assert not any("pg_advisory_unlock" in c for c in conn.calls)
    assert conn.closed


@pytest.mark.pg
def test_advisory_lock_excludes_a_second_connection(pg_conn):
    import psycopg

    from episteme.data.db.connection import dsn_from_settings

    with psycopg.connect(dsn_from_settings(), autocommit=True) as other:
        with lock.advisory_lock():
            row = other.execute(
                "select pg_try_advisory_lock(%s)", (lock.HEAVY_ADVISORY_KEY,)
            ).fetchone()
            assert row[0] is False
        row = other.execute(
            "select pg_try_advisory_lock(%s)", (lock.HEAVY_ADVISORY_KEY,)
        ).fetchone()
        assert row[0] is True
        other.execute("select pg_advisory_unlock(%s)", (lock.HEAVY_ADVISORY_KEY,))


@pytest.mark.pg
def test_advisory_lock_no_wait_raises_lockbusy(pg_conn):
    import psycopg

    from episteme.data.db.connection import dsn_from_settings

    with psycopg.connect(dsn_from_settings(), autocommit=True) as other:
        other.execute("select pg_advisory_lock(%s)", (lock.HEAVY_ADVISORY_KEY,))
        try:
            with pytest.raises(lock.LockBusy):
                with lock.advisory_lock(wait=False):
                    pass
        finally:
            other.execute("select pg_advisory_unlock(%s)", (lock.HEAVY_ADVISORY_KEY,))


@pytest.mark.pg
def test_advisory_lock_connection_is_tagged(pg_conn, monkeypatch):
    import psycopg

    seen = {}
    real = psycopg.connect

    def spy(*a, **kw):
        conn = real(*a, **kw)
        seen["name"] = conn.execute("show application_name").fetchone()[0]
        return conn

    monkeypatch.setattr(psycopg, "connect", spy)
    with lock.advisory_lock():
        pass
    assert seen["name"].startswith("episteme:")
    assert seen["name"].endswith(":lock")
    assert len(seen["name"].encode()) <= 63


def test_app_name_suffix_keeps_the_63_byte_limit(monkeypatch):
    from types import SimpleNamespace

    from episteme.data.db import connection as dbc

    name = dbc.app_name(SimpleNamespace(run_id="r" * 200), suffix=":lock")
    assert len(name.encode()) == 63
    assert name.endswith(f":{os.getpid()}:lock")


def _quiet_guard(monkeypatch):
    monkeypatch.setattr(stop, "install_stop_handlers", lambda: None)
    monkeypatch.setattr(stop, "start_parent_watchdog", lambda *a, **k: None)


def test_stage_guard_marks_the_advisory_wait_as_a_long_statement(monkeypatch):
    """M1: the status begins before the advisory-lock wait; the wait is a long
    statement, so a long wait is not reported as a stall."""
    from episteme.ops import status

    _env(monkeypatch, run_id="r-adv", runner_pid=os.getpid())
    _quiet_guard(monkeypatch)
    seen = {}

    @contextlib.contextmanager
    def fake_advisory(**kw):
        seen["wait"] = kw.get("wait")
        seen["during"] = status.RunStatus(status.run_dir("r-adv")).read()
        yield

    monkeypatch.setattr(lock, "advisory_lock", fake_advisory)
    with stop.stage_guard(lambda: None, lock_stage="load", db_lock=True):
        seen["body"] = status.RunStatus(status.run_dir("r-adv")).read()
    assert seen["wait"] is True
    assert seen["during"]["state"] == "running" and seen["during"]["stage"] == "load"
    assert seen["during"]["long_statement"]["label"] == "waiting for the advisory lock"
    assert seen["body"]["long_statement"] is None


def test_no_wait_busy_advisory_lock_exits_75_and_refused(monkeypatch):
    """M1: EPISTEME_NO_WAIT=1 passes wait=False; a busy lock is SystemExit(75)."""
    from episteme.ops import status

    _env(monkeypatch, run_id="r-nw", runner_pid=os.getpid())
    monkeypatch.setenv("EPISTEME_NO_WAIT", "1")
    get_settings.cache_clear()
    _quiet_guard(monkeypatch)
    seen = {}

    @contextlib.contextmanager
    def busy_advisory(**kw):
        seen["wait"] = kw.get("wait")
        raise lock.LockBusy({"stage": "advisory lock", "run_id": "another session"})
        yield  # pragma: no cover

    monkeypatch.setattr(lock, "advisory_lock", busy_advisory)
    with pytest.raises(SystemExit) as exc:
        with stop.stage_guard(lambda: None, lock_stage="load", db_lock=True):
            pytest.fail("the body must not run when the advisory lock is busy")
    assert exc.value.code == 75
    assert seen["wait"] is False
    assert status.RunStatus(status.run_dir("r-nw")).read()["state"] == "refused"
    assert not _heavy().exists()  # the heavy lock this stage took is released


def test_no_wait_busy_heavy_lock_exits_75(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("EPISTEME_NO_WAIT", "1")
    get_settings.cache_clear()
    _quiet_guard(monkeypatch)
    heavy = _heavy()
    heavy.write_text(json.dumps(_rec(run_id="other")), encoding="utf-8")
    monkeypatch.setattr(lock.time, "sleep", lambda s: pytest.fail("no-wait must not wait"))
    with pytest.raises(SystemExit) as exc:
        with stop.stage_guard(lambda: None, lock_stage="load"):
            pytest.fail("the body must not run while another run holds the lock")
    assert exc.value.code == 75
    assert json.loads(heavy.read_text(encoding="utf-8"))["run_id"] == "other"


def test_help_takes_no_lock_and_writes_no_status(monkeypatch):
    """--help must not wait behind a live heavy job, and writes no status."""
    from episteme.data.pmc import extract_pmc
    from episteme.ops import status

    _env(monkeypatch, run_id="r-help")
    monkeypatch.setattr(stop, "install_stop_handlers", lambda: None)
    monkeypatch.setattr(stop, "start_parent_watchdog", lambda *a, **k: None)
    _heavy().write_text(json.dumps(_rec(run_id="other")), encoding="utf-8")

    def no_wait(sec):
        raise AssertionError("--help waited for the lock")

    monkeypatch.setattr(lock.time, "sleep", no_wait)
    with pytest.raises(SystemExit) as exc:
        extract_pmc.main(["--help"])
    assert exc.value.code == 0
    assert json.loads(_heavy().read_text(encoding="utf-8"))["run_id"] == "other"
    assert not (status.run_dir("r-help") / "status.json").exists()
    # argv=None reads sys.argv[1:]
    monkeypatch.setattr(stop.sys, "argv", ["prog", "-h"])
    with stop.stage_guard(lambda: None, lock_stage="load"):
        pass
    assert not (status.run_dir("r-help") / "status.json").exists()
