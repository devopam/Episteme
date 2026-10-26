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


class FakeProc:
    """A stand-in for ``psutil.Process``. ``alive`` is the sequence of
    ``is_running()`` answers; the last answer repeats."""

    def __init__(self, pid, *, exe="", parent=None, alive=(True,), raise_on_poll=None):
        self.pid = pid
        self._exe = exe
        self._parent = parent
        self._alive = list(alive)
        self._raise = raise_on_poll

    def is_running(self):
        if self._raise is not None and len(self._alive) <= 1:
            raise self._raise
        return self._alive.pop(0) if len(self._alive) > 1 else self._alive[0]

    def exe(self):
        return self._exe

    def parent(self):
        return self._parent


def _fake_psutil(monkeypatch, procs):
    """``psutil.Process(pid)`` returns ``procs[pid]`` or raises NoSuchProcess.
    A bare ``pid_exists`` must not be polled (PID reuse)."""

    def _process(pid):
        if pid not in procs:
            raise stop.psutil.NoSuchProcess(pid)
        return procs[pid]

    def _no_pid_exists(pid):
        raise AssertionError("the watchdog must poll Process.is_running, not pid_exists")

    monkeypatch.setattr(stop.psutil, "Process", _process)
    monkeypatch.setattr(stop.psutil, "pid_exists", _no_pid_exists)


def _no_exit(monkeypatch):
    codes = []
    monkeypatch.setattr(stop.os, "_exit", lambda code: codes.append(code))
    return codes


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
    monkeypatch.setattr(stop.signal, "signal", lambda s, h: seen.setdefault(s, []).append(h))
    stop.install_stop_handlers()
    stop.install_stop_handlers()  # idempotent
    sigs = [signal.SIGTERM] + ([signal.SIGBREAK] if hasattr(signal, "SIGBREAK") else [])
    for sig in sigs:
        assert len(seen[sig]) == 2
        assert seen[sig][0] is seen[sig][1]
    handler = seen[signal.SIGTERM][0]
    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGTERM, None)


def test_watchdog_cancels_once_and_exits_143(monkeypatch):
    _fake_psutil(monkeypatch, {99: FakeProc(99, alive=[True, True, True, False])})
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
    _fake_psutil(monkeypatch, {99: FakeProc(99, alive=[False])})
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


def test_install_off_main_thread_warns(monkeypatch, caplog):
    def deny(s, h):
        raise ValueError("signal only works in main thread")

    monkeypatch.setattr(stop.signal, "signal", deny)
    with caplog.at_level("WARNING", logger=stop._LOG.name):
        th = threading.Thread(target=stop.install_stop_handlers)
        th.start()
        th.join()
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_watchdog_exits_even_if_cancel_hangs(monkeypatch):
    _fake_psutil(monkeypatch, {99: FakeProc(99, alive=[False])})
    codes = []

    def fake_exit(code):
        codes.append(code)
        raise SystemExit

    monkeypatch.setattr(stop.os, "_exit", fake_exit)
    monkeypatch.setattr(threading, "excepthook", lambda a: None)
    never = threading.Event()
    t = stop.start_parent_watchdog(never.wait, interval=0.01, parent_pid=99, cancel_timeout=0.1)
    t.join(5)
    never.set()
    assert codes == [143]


def test_stage_guard_resets_term_flag_on_entry(monkeypatch):
    monkeypatch.setattr(stop, "install_stop_handlers", lambda: None)
    monkeypatch.setattr(stop, "start_parent_watchdog", lambda *a, **k: None)
    monkeypatch.setattr(stop, "_TERM_SEEN", True)
    with pytest.raises(SystemExit) as ei:
        with stop.stage_guard(lambda: None):
            raise KeyboardInterrupt
    assert ei.value.code == 130


def test_stage_guard_maps_term_to_143(monkeypatch):
    monkeypatch.setattr(stop, "install_stop_handlers", lambda: None)
    monkeypatch.setattr(stop, "start_parent_watchdog", lambda *a, **k: None)
    with pytest.raises(SystemExit) as ei:
        with stop.stage_guard(lambda: None):
            stop._on_term(signal.SIGTERM, None)
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


def test_watchdog_thread_ends_when_the_guarded_stage_finishes(monkeypatch):
    monkeypatch.setattr(stop, "_watch_target", lambda pid: (FakeProc(7), 7))
    monkeypatch.setattr(stop, "install_stop_handlers", lambda: None)
    seen = []
    real = stop.start_parent_watchdog

    def _spy(cancel, **kw):
        t = real(cancel, interval=30.0, **kw)
        seen.append(t)
        return t

    monkeypatch.setattr(stop, "start_parent_watchdog", _spy)
    with stop.stage_guard(lambda: None):
        pass
    seen[0].join(5)
    assert not seen[0].is_alive()


def test_watch_target_explicit_parent_pid_wins(monkeypatch):
    _fake_psutil(monkeypatch, {111: FakeProc(111), 222: FakeProc(222)})
    monkeypatch.setattr(stop, "_runner_pid", lambda: 222)
    proc, pid = stop._watch_target(111)
    assert (proc.pid, pid) == (111, 111)


def test_watch_target_uses_a_running_runner_pid(monkeypatch):
    _fake_psutil(monkeypatch, {222: FakeProc(222)})
    monkeypatch.setattr(stop, "_runner_pid", lambda: 222)

    def _walk():
        raise AssertionError("the ancestor walk must not run when the runner is alive")

    monkeypatch.setattr(stop, "_ancestor_to_watch", _walk)
    proc, pid = stop._watch_target(None)
    assert (proc.pid, pid) == (222, 222)


def test_watch_target_stale_runner_pid_warns_and_falls_back(monkeypatch, caplog):
    # 222 is not running (NoSuchProcess); a reused PID (is_running False) is the same.
    _fake_psutil(monkeypatch, {333: FakeProc(333, alive=[False])})
    for stale in (222, 333):
        monkeypatch.setattr(stop, "_runner_pid", lambda stale=stale: stale)
        monkeypatch.setattr(stop, "_ancestor_to_watch", lambda: FakeProc(444))
        caplog.clear()
        with caplog.at_level("WARNING", logger=stop._LOG.name):
            proc, pid = stop._watch_target(None)
        assert (proc.pid, pid) == (444, 444)
        assert f"EPISTEME_RUNNER_PID={stale} is not running" in caplog.text


def test_stale_runner_pid_does_not_exit_at_once(monkeypatch):
    _fake_psutil(monkeypatch, {})
    monkeypatch.setattr(stop, "_runner_pid", lambda: 222)
    monkeypatch.setattr(stop, "_ancestor_to_watch", lambda: FakeProc(444))
    codes = _no_exit(monkeypatch)
    done = threading.Event()
    t = stop.start_parent_watchdog(lambda: None, interval=0.01, stop=done)
    time.sleep(0.2)
    done.set()
    t.join(5)
    assert codes == []


@pytest.mark.parametrize("runner", [None, 222], ids=["no-runner", "stale-runner"])
def test_nothing_to_watch_runs_without_a_watchdog(monkeypatch, caplog, runner):
    _fake_psutil(monkeypatch, {})
    monkeypatch.setattr(stop, "_runner_pid", lambda: runner)
    monkeypatch.setattr(stop, "_ancestor_to_watch", lambda: None)
    codes = _no_exit(monkeypatch)
    cancels: list[int] = []
    done = threading.Event()
    with caplog.at_level("WARNING", logger=stop._LOG.name):
        t = stop.start_parent_watchdog(lambda: cancels.append(1), interval=0.01, stop=done)
        time.sleep(0.2)
    assert codes == [] and cancels == []
    assert "running without a parent watchdog" in caplog.text
    done.set()
    t.join(5)
    assert not t.is_alive()


def test_runner_pid_comes_from_settings(monkeypatch):
    import episteme.config as cfg

    monkeypatch.setattr(cfg, "get_settings", lambda: replace(get_settings(), runner_pid=5150))
    assert stop._runner_pid() == 5150


def test_runner_pid_settings_error_gives_none(monkeypatch, caplog):
    import episteme.config as cfg

    def _boom():
        raise cfg.ConfigError("bad")

    monkeypatch.setattr(cfg, "get_settings", _boom)
    with caplog.at_level("WARNING", logger=stop._LOG.name):
        assert stop._runner_pid() is None
    assert "runner PID unavailable" in caplog.text


def test_ancestor_walk_skips_the_venv_launcher(monkeypatch):
    bash = FakeProc(30, exe=r"C:\Program Files\Git\usr\bin\bash.exe")
    launcher2 = FakeProc(21, exe=stop.sys.executable.upper(), parent=bash)  # case differs
    launcher = FakeProc(20, exe=stop.sys.executable, parent=launcher2)
    me = FakeProc(stop.os.getpid(), exe=r"C:\uv\python\python.exe", parent=launcher)
    _fake_psutil(monkeypatch, {me.pid: me})
    if stop.sys.platform != "win32":  # normcase only folds case on Windows
        launcher2._exe = stop.sys.executable
    assert stop._ancestor_to_watch() is bash


def test_ancestor_walk_skips_the_prefix_launcher_path(monkeypatch):
    bash = FakeProc(30, exe="/usr/bin/bash")
    launcher = FakeProc(
        20, exe=stop.os.path.join(stop.sys.prefix, "Scripts", "python.exe"), parent=bash
    )
    me = FakeProc(stop.os.getpid(), parent=launcher)
    _fake_psutil(monkeypatch, {me.pid: me})
    assert stop._ancestor_to_watch() is bash


def test_ancestor_walk_without_launcher_is_the_parent(monkeypatch):
    bash = FakeProc(30, exe="/usr/bin/bash")
    me = FakeProc(stop.os.getpid(), exe="/usr/bin/python3", parent=bash)
    _fake_psutil(monkeypatch, {me.pid: me})
    assert stop._ancestor_to_watch() is bash


def test_watchdog_treats_nosuchprocess_on_poll_as_gone(monkeypatch, caplog):
    gone = stop.psutil.NoSuchProcess(99)
    _fake_psutil(monkeypatch, {99: FakeProc(99, alive=[True, True], raise_on_poll=gone)})
    codes = _no_exit(monkeypatch)
    with caplog.at_level("WARNING", logger=stop._LOG.name):
        t = stop.start_parent_watchdog(lambda: None, interval=0.01, parent_pid=99)
        t.join(5)
    assert codes == [143]
    assert "treated as gone" in caplog.text


def test_watchdog_explicit_parent_gone_at_start_exits(monkeypatch):
    _fake_psutil(monkeypatch, {})
    codes = _no_exit(monkeypatch)
    t = stop.start_parent_watchdog(lambda: None, interval=0.01, parent_pid=99)
    t.join(5)
    assert codes == [143]


# --- the stop file (run_pipeline.sh writes it on INT/TERM) ---------------------


def test_stop_file_runs_the_sigterm_handler_in_the_main_thread(monkeypatch, tmp_path):
    # Windows: bash cannot signal a native Python, so the runner writes a file.
    monkeypatch.setattr(stop, "_TERM_SEEN", False)
    old = signal.signal(signal.SIGTERM, stop._on_term)
    codes = _no_exit(monkeypatch)
    alive = FakeProc(99)
    monkeypatch.setattr(stop, "_watch_target", lambda pid: (alive, 99))
    request = tmp_path / "run" / "stop.request"
    done = threading.Event()
    t = None
    try:
        t = stop.start_parent_watchdog(
            lambda: None, interval=30.0, stop=done, stop_file=request, stop_poll=0.05
        )
        t0 = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            request.parent.mkdir()
            request.write_text("TERM\n", encoding="utf-8")
            while time.monotonic() - t0 < 3:
                time.sleep(0.01)
        assert time.monotonic() - t0 < 3
        assert stop._TERM_SEEN is True
        assert threading.current_thread() is threading.main_thread()
        assert codes == []  # no os._exit on the stop-file path
    finally:
        done.set()
        if t is not None:
            t.join(5)
        signal.signal(signal.SIGTERM, old)


def test_stop_file_is_delivered_once(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(stop._thread, "interrupt_main", lambda *a: calls.append(a))
    codes = _no_exit(monkeypatch)
    monkeypatch.setattr(stop, "_watch_target", lambda pid: (FakeProc(99), 99))
    request = tmp_path / "stop.request"
    request.write_text("INT\n", encoding="utf-8")
    done = threading.Event()
    t = stop.start_parent_watchdog(
        lambda: None, interval=30.0, stop=done, stop_file=request, stop_poll=0.01
    )
    time.sleep(0.3)
    done.set()
    t.join(5)
    assert calls == [(signal.SIGTERM,)]
    assert codes == []


def test_stop_file_cancels_the_statement_after_a_grace_period(monkeypatch, tmp_path):
    # A main thread inside a long statement sees the interrupt only when the
    # C call returns: the watchdog then cancels the statement.
    monkeypatch.setattr(stop._thread, "interrupt_main", lambda *a: None)
    _no_exit(monkeypatch)
    monkeypatch.setattr(stop, "_watch_target", lambda pid: (FakeProc(99), 99))
    request = tmp_path / "stop.request"
    request.write_text("TERM\n", encoding="utf-8")
    cancels = []
    done = threading.Event()
    t = stop.start_parent_watchdog(
        lambda: cancels.append(1),
        interval=30.0,
        stop=done,
        stop_file=request,
        stop_poll=0.01,
        cancel_after=0.1,
    )
    deadline = time.monotonic() + 3
    while not cancels and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)
    done.set()
    t.join(5)
    assert cancels == [1]


def test_stop_file_is_polled_without_a_parent_to_watch(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(stop._thread, "interrupt_main", lambda *a: calls.append(a))
    _fake_psutil(monkeypatch, {})
    monkeypatch.setattr(stop, "_runner_pid", lambda: None)
    monkeypatch.setattr(stop, "_ancestor_to_watch", lambda: None)
    codes = _no_exit(monkeypatch)
    request = tmp_path / "stop.request"
    done = threading.Event()
    t = stop.start_parent_watchdog(
        lambda: None, interval=30.0, stop=done, stop_file=request, stop_poll=0.01
    )
    request.write_text("TERM\n", encoding="utf-8")
    deadline = time.monotonic() + 3
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    done.set()
    t.join(5)
    assert calls == [(signal.SIGTERM,)]
    assert codes == []


def test_stop_file_comes_from_settings(monkeypatch, tmp_path):
    import episteme.config as cfg

    path = tmp_path / "stop.request"
    monkeypatch.setattr(cfg, "get_settings", lambda: replace(get_settings(), stop_file=path))
    assert stop._stop_file() == path


def test_parent_gone_terminates_the_child_processes_before_exit(monkeypatch, tmp_path):
    import subprocess
    import sys

    import psutil

    # Only a process this test starts; the fake "self" lists only it.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        mine = psutil.Process(child.pid)

        class Me:
            def children(self, recursive=False):
                assert recursive
                return [mine]

        monkeypatch.setattr(stop, "_self_process", lambda: Me())
        monkeypatch.setattr(stop, "_watch_target", lambda pid: (FakeProc(99, alive=[False]), 99))
        seen = []

        def fake_exit(code):
            seen.append((code, child.poll()))

        monkeypatch.setattr(stop.os, "_exit", fake_exit)
        t = stop.start_parent_watchdog(lambda: None, interval=0.01, parent_pid=99)
        t.join(20)
        assert len(seen) == 1
        code, child_rc = seen[0]
        assert code == 143
        assert child_rc is not None, "the child was still running at the exit"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(10)


def test_child_cleanup_errors_do_not_block_the_exit(monkeypatch):
    def boom():
        raise RuntimeError("no process table")

    monkeypatch.setattr(stop, "_self_process", boom)
    monkeypatch.setattr(stop, "_watch_target", lambda pid: (FakeProc(99, alive=[False]), 99))
    codes = _no_exit(monkeypatch)
    t = stop.start_parent_watchdog(lambda: None, interval=0.01, parent_pid=99)
    t.join(5)
    assert codes == [143]
