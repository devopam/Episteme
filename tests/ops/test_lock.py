"""The heavy-job lock and the download lock (ops/lock.py)."""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import queue
import subprocess
import sys
import time
from pathlib import Path

import pytest

from episteme.ops import lock


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _write(path, **fields):
    rec = {
        "kind": "heavy",
        "pid": os.getpid(),
        "create_time": lock._create_time(os.getpid()),
        "run_id": "r0",
        "stage": "s",
        "started_at": "2026-10-20T00:00:00+00:00",
        "state": "running",
    }
    rec.update(fields)
    path.write_text(json.dumps(rec), encoding="utf-8")
    return rec


def test_lock_dir_is_redirected_in_tests(tmp_path_factory):
    base = tmp_path_factory.getbasetemp().resolve()
    assert base in lock.lock_dir().resolve().parents
    env_dir = Path(os.environ["EPISTEME_LOCK_DIR"]).resolve()
    assert base in env_dir.parents
    from episteme.config import get_settings

    assert get_settings().lock_dir.resolve() == env_dir


def test_acquire_empty_folder_writes_record(tmp_path):
    assert lock.acquire("heavy", "r1", "load", d=tmp_path) == "acquired"
    rec = json.loads((tmp_path / "heavy.lock").read_text(encoding="utf-8"))
    assert rec["pid"] == os.getpid()
    assert rec["create_time"] is not None
    assert rec["run_id"] == "r1"
    assert rec["stage"] == "load"
    assert rec["started_at"]
    assert rec["state"] == "running"


def test_second_run_busy_same_run_reentrant(tmp_path):
    assert lock.try_acquire("heavy", "r1", "a", d=tmp_path)[0] == "acquired"
    status, holder = lock.try_acquire("heavy", "r2", "b", d=tmp_path)
    assert status == "busy"
    assert holder["run_id"] == "r1"
    assert lock.try_acquire("heavy", "r1", "a", d=tmp_path)[0] == "reentrant"


def test_release_wrong_run_keeps_file(tmp_path):
    lock.acquire("heavy", "r1", "a", d=tmp_path)
    assert lock.release("heavy", "r2", d=tmp_path) is False
    assert (tmp_path / "heavy.lock").exists()
    assert lock.release("heavy", "r1", d=tmp_path) is True
    assert not (tmp_path / "heavy.lock").exists()


def test_dead_owner_is_taken_over_and_logged(tmp_path, caplog):
    _write(tmp_path / "heavy.lock", pid=_dead_pid(), create_time=1.0)
    with caplog.at_level(logging.WARNING, logger="episteme.ops.lock"):
        status, rec = lock.try_acquire("heavy", "r1", "a", d=tmp_path)
    assert status == "acquired"
    assert rec["run_id"] == "r1"
    assert "took over a stale heavy.lock" in caplog.text


def test_pid_reuse_counts_as_stale(tmp_path):
    ct = lock._create_time(os.getpid())
    _write(tmp_path / "heavy.lock", create_time=ct - 100)
    assert lock.try_acquire("heavy", "r1", "a", d=tmp_path)[0] == "acquired"


def test_unreadable_file_grace(tmp_path):
    path = tmp_path / "heavy.lock"
    path.write_text("{not json", encoding="utf-8")
    assert lock.try_acquire("heavy", "r1", "a", d=tmp_path)[0] == "busy"
    old = time.time() - 120
    os.utime(path, (old, old))
    assert lock.try_acquire("heavy", "r1", "a", d=tmp_path)[0] == "acquired"


def test_no_wait_raises_with_holder(tmp_path):
    lock.acquire("heavy", "r1", "a", d=tmp_path)
    with pytest.raises(lock.LockBusy) as err:
        lock.acquire("heavy", "r2", "b", wait=False, d=tmp_path)
    assert err.value.holder["run_id"] == "r1"


def test_wait_polls_until_released(tmp_path):
    lock.acquire("heavy", "r1", "a", d=tmp_path)
    calls = []

    def fake_sleep(_sec):
        calls.append(1)
        if len(calls) == 2:
            lock.release("heavy", "r1", d=tmp_path)

    assert lock.acquire("heavy", "r2", "b", d=tmp_path, sleep=fake_sleep) == "acquired"
    assert len(calls) == 2


def _race_target(d, run_id, barrier, out, rounds):
    from pathlib import Path as _Path

    from episteme.ops import lock as _lock

    for r in range(rounds):
        barrier.wait(timeout=60)
        status, _rec = _lock.try_acquire("heavy", run_id, "race", d=_Path(d) / str(r))
        out.put((r, run_id, status))


def test_two_process_race_has_one_winner(tmp_path):
    rounds = 20
    dead = _dead_pid()
    for r in range(rounds):
        (tmp_path / str(r)).mkdir()
        _write(tmp_path / str(r) / "heavy.lock", pid=dead, create_time=1.0)
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    out = ctx.Queue()
    procs = [
        ctx.Process(target=_race_target, args=(str(tmp_path), name, barrier, out, rounds))
        for name in ("a", "b")
    ]
    for p in procs:
        p.start()
    deadline = time.monotonic() + 120
    results = []
    try:
        while len(results) < rounds * 2:
            left = deadline - time.monotonic()
            try:
                results.append(out.get(timeout=max(0.1, min(left, 5))))
            except queue.Empty:
                codes = [p.exitcode for p in procs]
                if left <= 0 or any(c not in (None, 0) for c in codes):
                    pytest.fail(f"race workers stalled: {len(results)} results, exit codes {codes}")
    finally:
        for p in procs:
            p.join(timeout=10)
            if p.is_alive():
                p.terminate()
    for r in range(rounds):
        statuses = sorted(s for rr, _n, s in results if rr == r)
        assert statuses == ["acquired", "busy"], (r, statuses)


def test_download_blocked_by_heavy_lock_and_request(tmp_path):
    lock.acquire("heavy", "h", "a", d=tmp_path)
    with pytest.raises(lock.LockBusy):
        lock.acquire("download", "dl", "x", wait=False, d=tmp_path)
    lock.release("heavy", "h", d=tmp_path)
    _write(tmp_path / "heavy.request", run_id="h")
    with pytest.raises(lock.LockBusy):
        lock.acquire("download", "dl", "x", wait=False, d=tmp_path)


def test_heavy_blocked_by_running_download_not_paused(tmp_path):
    lock.acquire("download", "dl", "x", d=tmp_path)
    with pytest.raises(lock.LockBusy):
        lock.acquire("heavy", "h", "a", wait=False, d=tmp_path)
    rec = lock.read(tmp_path / "download.lock")
    rec["state"] = "paused"
    (tmp_path / "download.lock").write_text(json.dumps(rec), encoding="utf-8")
    assert lock.acquire("heavy", "h", "a", wait=False, d=tmp_path) == "acquired"


def test_waiting_heavy_posts_and_clears_request(tmp_path):
    lock.acquire("download", "dl", "x", d=tmp_path)
    seen = []

    def fake_sleep(_sec):
        seen.append(lock.read(tmp_path / "heavy.request"))
        if len(seen) == 2:
            lock.release("download", "dl", d=tmp_path)

    assert lock.acquire("heavy", "h", "a", d=tmp_path, sleep=fake_sleep) == "acquired"
    assert seen[0]["run_id"] == "h"
    assert not (tmp_path / "heavy.request").exists()


def test_request_cleared_when_wait_raises(tmp_path):
    lock.acquire("download", "dl", "x", d=tmp_path)

    def boom(_sec):
        raise RuntimeError("stop")

    with pytest.raises(RuntimeError):
        lock.acquire("heavy", "h", "a", d=tmp_path, sleep=boom)
    assert not (tmp_path / "heavy.request").exists()


def test_held_releases_only_what_it_acquired():
    with lock.held("heavy", "r1", "a"):
        assert lock.read(lock.lock_dir() / "heavy.lock")["run_id"] == "r1"
        with lock.held("heavy", "r1", "a") as status:
            assert status == "reentrant"
        assert (lock.lock_dir() / "heavy.lock").exists()
    assert not (lock.lock_dir() / "heavy.lock").exists()


def test_pause_point_no_heavy_returns_false(tmp_path):
    lock.acquire("download", "dl", "x", d=tmp_path)
    assert lock.pause_point("dl", d=tmp_path) is False


def test_pause_point_pauses_and_resumes(tmp_path):
    lock.acquire("download", "dl", "x", d=tmp_path)
    _write(tmp_path / "heavy.request", run_id="h")
    states = []

    def fake_sleep(_sec):
        states.append(lock.read(tmp_path / "download.lock")["state"])
        if len(states) == 2:
            (tmp_path / "heavy.request").unlink()

    assert lock.pause_point("dl", d=tmp_path, sleep=fake_sleep) is True
    assert states == ["paused", "paused"]
    assert lock.read(tmp_path / "download.lock")["state"] == "running"


def test_cli_acquire_busy_and_status(capsys):
    assert lock.main(["acquire", "heavy", "--run-id", "r1", "--stage", "load"]) == 0
    assert capsys.readouterr().out.strip() == "acquired"
    rc = lock.main(["acquire", "heavy", "--run-id", "r2", "--no-wait"])
    captured = capsys.readouterr()
    assert rc == lock.EXIT_BUSY == 75
    assert "r1" in captured.err
    assert lock.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "heavy.lock" in out and "download.lock" in out and "heavy.request" in out
    assert "r1" in out
    assert lock.main(["release", "heavy", "--run-id", "r1"]) == 0


def _fail_n_times(real, n):
    calls = {"n": 0}

    def wrapper(*args, **kwargs):
        if calls["n"] < n:
            calls["n"] += 1
            raise PermissionError(13, "in use")
        return real(*args, **kwargs)

    return wrapper


def test_release_retries_permission_error(tmp_path, monkeypatch):
    lock.acquire("heavy", "r1", "a", d=tmp_path)
    monkeypatch.setattr(os, "unlink", _fail_n_times(os.unlink, 2))
    assert lock.release("heavy", "r1", d=tmp_path) is True
    assert not (tmp_path / "heavy.lock").exists()


def test_release_gives_up_without_raising(tmp_path, monkeypatch, caplog):
    lock.acquire("heavy", "r1", "a", d=tmp_path)
    monkeypatch.setattr(os, "unlink", _fail_n_times(os.unlink, 99))
    with caplog.at_level(logging.ERROR, logger="episteme.ops.lock"):
        assert lock.release("heavy", "r1", d=tmp_path) is False
    assert "could not release" in caplog.text
    assert (tmp_path / "heavy.lock").exists()


def test_acquire_returns_acquired_when_request_clear_is_blocked(tmp_path, monkeypatch):
    lock.acquire("download", "dl", "x", d=tmp_path)

    def sleep(_sec):
        lock.release("download", "dl", d=tmp_path)
        monkeypatch.setattr(os, "unlink", _fail_n_times(os.unlink, 2))

    assert lock.acquire("heavy", "h", "a", d=tmp_path, sleep=sleep) == "acquired"
    assert not (tmp_path / "heavy.request").exists()


def test_acquire_survives_request_clear_that_never_works(tmp_path, monkeypatch, caplog):
    lock.acquire("download", "dl", "x", d=tmp_path)

    def sleep(_sec):
        lock.release("download", "dl", d=tmp_path)
        monkeypatch.setattr(os, "unlink", _fail_n_times(os.unlink, 99))

    with caplog.at_level(logging.ERROR, logger="episteme.ops.lock"):
        assert lock.acquire("heavy", "h", "a", d=tmp_path, sleep=sleep) == "acquired"
    assert "could not remove heavy.request" in caplog.text


def test_pause_point_resumes_when_replace_fails_twice(tmp_path, monkeypatch):
    lock.acquire("download", "dl", "x", d=tmp_path)
    _write(tmp_path / "heavy.request", run_id="h")
    monkeypatch.setattr(os, "replace", _fail_n_times(os.replace, 2))

    def fake_sleep(_sec):
        (tmp_path / "heavy.request").unlink()

    assert lock.pause_point("dl", d=tmp_path, sleep=fake_sleep) is True
    assert lock.read(tmp_path / "download.lock")["state"] == "running"


def test_locked_file_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "heavy.lock"
    _write(path, pid=_dead_pid(), create_time=1.0)
    old = time.time() - 3600
    os.utime(path, (old, old))

    def deny(self, *args, **kwargs):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(type(path), "read_text", deny)
    assert lock.read(path) is lock._LOCKED
    status, _rec = lock.try_acquire("heavy", "r1", "a", d=tmp_path)
    assert status == "busy"
    assert path.exists()
    with pytest.raises(lock.LockBusy):
        lock.acquire("download", "dl", "x", wait=False, d=tmp_path)


def test_stale_request_is_ignored(tmp_path):
    _write(tmp_path / "heavy.request", pid=_dead_pid(), create_time=1.0, run_id="h")
    assert lock.acquire("download", "dl", "x", wait=False, d=tmp_path) == "acquired"
    assert lock.pause_point("dl", d=tmp_path) is False
