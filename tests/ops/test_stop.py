import signal
import threading
import time
from dataclasses import replace

import pytest

from episteme.config import get_settings
from episteme.data.db import connection as dbc
from episteme.ops import stop


def _settings(run_id):
    return replace(get_settings(), run_id=run_id)


def test_app_name_format(monkeypatch):
    monkeypatch.setattr(dbc.os, "getpid", lambda: 4242)
    assert dbc.app_name(_settings("run-1")) == "episteme:run-1:4242"
    assert dbc.app_name(_settings(None)) == "episteme:adhoc:4242"


def test_app_name_truncates_run_id_first(monkeypatch):
    monkeypatch.setattr(dbc.os, "getpid", lambda: 4242)
    name = dbc.app_name(_settings("r" * 200))
    assert len(name.encode()) <= 63
    assert name.startswith("episteme:r")
    assert name.endswith(":4242")


def test_app_name_truncates_multibyte(monkeypatch):
    monkeypatch.setattr(dbc.os, "getpid", lambda: 4242)
    name = dbc.app_name(_settings("é" * 100))
    assert len(name.encode()) <= 63
    assert name.endswith(":4242")


def test_connect_kwargs_carry_application_name(monkeypatch):
    monkeypatch.setattr(dbc.os, "getpid", lambda: 7)
    kw = dbc.connect_kwargs(_settings("rid"))
    assert kw["application_name"] == "episteme:rid:7"


def test_install_stop_handlers_maps_sigterm(monkeypatch):
    seen = {}
    monkeypatch.setattr(stop.signal, "signal", lambda s, h: seen.setdefault(s, h))
    stop.install_stop_handlers()
    stop.install_stop_handlers()  # idempotent
    handler = seen[signal.SIGTERM]
    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGTERM, None)


def test_watchdog_cancels_once_and_exits_143(monkeypatch):
    states = iter([True, True, False])
    monkeypatch.setattr(stop.psutil, "pid_exists", lambda pid: next(states, False))
    calls = []

    class Exit(Exception):
        pass

    def fake_exit(code):
        calls.append(code)
        raise Exit

    monkeypatch.setattr(stop.os, "_exit", fake_exit)
    cancelled = []
    monkeypatch.setattr(threading, "excepthook", lambda a: None)
    t = stop.start_parent_watchdog(lambda: cancelled.append(1), interval=0.01, parent_pid=99)
    t.join(5)
    assert not t.is_alive()
    assert cancelled == [1]
    assert calls == [143]


def test_watchdog_swallows_cancel_errors(monkeypatch):
    monkeypatch.setattr(stop.psutil, "pid_exists", lambda pid: False)
    codes = []

    class Exit(Exception):
        pass

    def fake_exit(code):
        codes.append(code)
        raise Exit

    monkeypatch.setattr(stop.os, "_exit", fake_exit)

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(threading, "excepthook", lambda a: None)
    t = stop.start_parent_watchdog(boom, interval=0.01, parent_pid=99)
    t.join(5)
    assert codes == [143]


def test_stage_guard_maps_keyboardinterrupt_to_130(monkeypatch):
    monkeypatch.setattr(stop, "install_stop_handlers", lambda: None)
    monkeypatch.setattr(stop, "start_parent_watchdog", lambda *a, **k: None)
    monkeypatch.setattr(stop, "_TERM_SEEN", False)
    with pytest.raises(SystemExit) as ei:
        with stop.stage_guard(lambda: None):
            raise KeyboardInterrupt
    assert ei.value.code == 130


def test_stage_guard_maps_term_to_143(monkeypatch):
    monkeypatch.setattr(stop, "install_stop_handlers", lambda: None)
    monkeypatch.setattr(stop, "start_parent_watchdog", lambda *a, **k: None)
    monkeypatch.setattr(stop, "_TERM_SEEN", True)
    with pytest.raises(SystemExit) as ei:
        with stop.stage_guard(lambda: None):
            raise KeyboardInterrupt
    assert ei.value.code == 143


def test_stage_guard_cancel_uses_cancel_safe(monkeypatch):
    got = {}
    monkeypatch.setattr(stop, "install_stop_handlers", lambda: None)

    def fake_watchdog(cancel, **k):
        got["cancel"] = cancel

    monkeypatch.setattr(stop, "start_parent_watchdog", fake_watchdog)

    class C:
        n = 0

        def cancel_safe(self):
            self.n += 1

    c = C()
    with stop.stage_guard(lambda: c):
        got["cancel"]()
    assert c.n == 1


@pytest.mark.pg
def test_cancel_safe_ends_query_with_querycanceled(pg_conn):
    import os

    import psycopg

    dsn = os.environ["TEST_PG_DSN"]
    name = f"episteme:test-cancel:{time.time_ns() % 10**6}"
    conn = psycopg.connect(dsn, application_name=name, autocommit=True)
    out = {}

    def run():
        t0 = time.monotonic()
        try:
            conn.execute("SELECT pg_sleep(30)")
        except psycopg.errors.QueryCanceled as e:
            out["err"] = e
        out["dt"] = time.monotonic() - t0

    th = threading.Thread(target=run)
    th.start()
    time.sleep(1)
    conn.cancel_safe()
    th.join(10)
    conn.close()
    assert isinstance(out.get("err"), psycopg.errors.QueryCanceled)
    assert out["dt"] < 6


@pytest.mark.pg
def test_pool_connection_is_tagged(pg_conn):
    import os

    from episteme.data.db import connection as db

    with db.connection() as c:
        got = c.execute("show application_name").fetchone()[0]
    assert got.startswith("episteme:")
    assert got.endswith(f":{os.getpid()}")
