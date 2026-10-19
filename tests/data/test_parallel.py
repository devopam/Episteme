"""run_per_file: in-process for one worker, spawned processes (or threads) above
one; it stops at the first failed unit and resubmits transient failures.

Every helper here is module-level: a spawned child imports this module by name.
Every multi-worker test takes ``spawn_child_env`` (see tests/conftest.py)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from episteme.data.checkpoint_markers import input_key
from episteme.data.parallel import first_failure, run_per_file
from episteme.ops.retry import TransientUnitError


def _echo(path: Path, *, raw_dir: Path, tag: str) -> dict:
    return {"source_file": path.name, "skipped": False, "ok": True, "tag": tag, "pid": os.getpid()}


def _boom_on_b(path: Path, *, raw_dir: Path) -> dict:
    if path.name == "b.txt":
        raise RuntimeError("kaboom")
    return {"source_file": path.name, "skipped": False, "ok": True}


def _exit_on_b(path: Path, *, raw_dir: Path) -> dict:
    if path.name == "b.txt":
        os._exit(3)
    return {"source_file": path.name, "skipped": False, "ok": True}


def _exit_on_b_others_slow(path: Path, *, raw_dir: Path) -> dict:
    """b kills its worker at once; every other file is still running then."""
    if path.name == "b.txt":
        os._exit(3)
    time.sleep(3)
    return {"source_file": path.name, "skipped": False, "ok": True}


def _transient_then_exit(path: Path, *, raw_dir: Path, counter: Path) -> dict:
    """First call: a transient error. The retry kills its worker."""
    n = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
    counter.write_text(str(n + 1), encoding="utf-8")
    if n == 0:
        raise TransientUnitError(path.name, "OSError(5, 'I/O error')")
    os._exit(3)


def _fail_on_b(path: Path, *, raw_dir: Path, log: Path | None = None) -> dict:
    """b.txt returns a failed result (the common case: process_one caught it)."""
    if log is not None:  # one file per call: concurrent appends are not atomic on Windows
        log.mkdir(exist_ok=True)
        (log / path.name).touch()
    if path.name == "b.txt":
        return {"source_file": path.name, "skipped": False, "ok": False, "error": "bad"}
    if path.name != "a.txt":
        time.sleep(0.3)
    return {"source_file": path.name, "skipped": False, "ok": True}


def _transient_twice(path: Path, *, raw_dir: Path, counter: Path) -> dict:
    n = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
    counter.write_text(str(n + 1), encoding="utf-8")
    if n < 2:
        raise TransientUnitError(path.name, "OSError(5, 'I/O error')")
    return {"source_file": path.name, "skipped": False, "ok": True}


def _always_transient(path: Path, *, raw_dir: Path) -> dict:
    raise TransientUnitError(path.name, "TimeoutError()")


def _probe_child(path: Path, *, raw_dir: Path) -> dict:
    from episteme.config import get_settings
    from episteme.data.db.connection import dsn_from_settings
    from episteme.data.db.guard import ProductionDatabaseGuardError

    try:
        dsn_from_settings()
        refused = False
    except ProductionDatabaseGuardError:
        refused = True
    return {
        "source_file": path.name,
        "skipped": False,
        "ok": True,
        "refused": refused,
        "processed_root": str(get_settings().processed_root),
        "pid": os.getpid(),
    }


def _files(tmp_path: Path, names=("a.txt", "b.txt", "c.txt", "d.txt")):
    raw = tmp_path / "raw"
    raw.mkdir()
    out = []
    for n in names:
        p = raw / n
        p.write_text(n, encoding="utf-8")
        out.append(p)
    return raw, out


def test_one_worker_runs_in_order_in_process(tmp_path):
    raw, files = _files(tmp_path)
    seen: list[dict] = []
    results = run_per_file(_echo, files, workers=1, on_result=seen.append, raw_dir=raw, tag="t")
    assert [r["source_file"] for r in results] == ["a.txt", "b.txt", "c.txt", "d.txt"]
    assert {r["pid"] for r in results} == {os.getpid()}
    assert seen == results


def test_three_workers_same_results_in_child_processes(tmp_path, spawn_child_env):
    raw, files = _files(tmp_path)
    seen: list[dict] = []
    one = run_per_file(_echo, files, workers=1, raw_dir=raw, tag="t")
    three = run_per_file(_echo, files, workers=3, on_result=seen.append, raw_dir=raw, tag="t")

    def strip(rs):
        return sorted((r["source_file"], r["tag"], r["ok"]) for r in rs)

    assert strip(three) == strip(one)
    assert os.getpid() not in {r["pid"] for r in three}
    # on_result ran in this (parent) process: seen was filled here
    assert strip(seen) == strip(three)


def test_escaped_exception_becomes_a_failed_result(tmp_path, spawn_child_env):
    raw, files = _files(tmp_path)
    results = run_per_file(_boom_on_b, files, workers=3, raw_dir=raw, stop_on_failure=False)
    by = {r["source_file"]: r for r in results}
    assert set(by) == {"a.txt", "b.txt", "c.txt", "d.txt"}
    assert by["b.txt"]["ok"] is False
    assert by["b.txt"]["skipped"] is False
    assert by["b.txt"]["error"].startswith("worker: ")
    assert "kaboom" in by["b.txt"]["error"]
    assert all(by[n]["ok"] for n in ("a.txt", "c.txt", "d.txt"))


def test_dead_worker_fails_the_remaining_files_without_raising(tmp_path, spawn_child_env):
    raw, files = _files(tmp_path)
    results = run_per_file(_exit_on_b, files, workers=2, raw_dir=raw, stop_on_failure=False)
    assert sorted(r["source_file"] for r in results) == ["a.txt", "b.txt", "c.txt", "d.txt"]
    b = next(r for r in results if r["source_file"] == "b.txt")
    assert b["ok"] is False
    assert b["error"].startswith("worker: ")


def test_failed_result_uses_input_key_relative_to_raw_dir(tmp_path, spawn_child_env):
    raw = tmp_path / "raw"
    (raw / "sub").mkdir(parents=True)
    p = raw / "sub" / "b.txt"
    p.write_text("x", encoding="utf-8")
    [r] = run_per_file(_boom_on_b, [p], workers=2, raw_dir=raw)
    assert r["source_file"] == input_key(p, raw)


def test_no_files_returns_empty_without_starting_a_pool(tmp_path, monkeypatch):
    import episteme.data.parallel as par

    def _no_pool(*_a, **_k):
        raise AssertionError("pool started for zero files")

    monkeypatch.setattr(par, "ProcessPoolExecutor", _no_pool)
    assert run_per_file(_echo, [], workers=3, raw_dir=tmp_path, tag="t") == []


def test_more_workers_than_files(tmp_path, spawn_child_env):
    raw, files = _files(tmp_path, ("a.txt",))
    [r] = run_per_file(_echo, files, workers=8, raw_dir=raw, tag="t")
    assert r["ok"] is True


@pytest.mark.parametrize("bad", [0, -4])
def test_workers_below_one_runs_in_process(tmp_path, bad):
    raw, files = _files(tmp_path, ("a.txt", "b.txt"))
    results = run_per_file(_echo, files, workers=bad, raw_dir=raw, tag="t")
    assert {r["pid"] for r in results} == {os.getpid()}


def test_spawned_child_is_kept_off_any_database(tmp_path, spawn_child_env):
    raw, files = _files(tmp_path, ("a.txt", "b.txt"))
    results = run_per_file(_probe_child, files, workers=2, raw_dir=raw)
    assert len(results) == 2
    for r in results:
        assert r["pid"] != os.getpid()
        assert r["refused"] is True
        assert Path(r["processed_root"]) == spawn_child_env


# --- stop at the first failure (operating standard section 6.2) ---------------


def test_one_worker_stops_after_the_failing_second_file(tmp_path):
    raw, files = _files(tmp_path, ("a.txt", "b.txt", "c.txt"))
    log = tmp_path / "calls"
    results = run_per_file(_fail_on_b, files, workers=1, raw_dir=raw, log=log)
    assert sorted(x.name for x in log.iterdir()) == ["a.txt", "b.txt"]  # c never called
    assert [r["source_file"] for r in results] == ["a.txt", "b.txt"]
    assert first_failure(results)["source_file"] == "b.txt"


def test_one_worker_continues_when_stop_on_failure_is_false(tmp_path):
    raw, files = _files(tmp_path, ("a.txt", "b.txt", "c.txt"))
    results = run_per_file(_fail_on_b, files, workers=1, raw_dir=raw, stop_on_failure=False)
    assert [r["source_file"] for r in results] == ["a.txt", "b.txt", "c.txt"]


def test_one_worker_escaped_exception_becomes_a_failed_result_and_stops(tmp_path):
    raw, files = _files(tmp_path, ("a.txt", "b.txt", "c.txt"))
    results = run_per_file(_boom_on_b, files, workers=1, raw_dir=raw)
    assert [r["source_file"] for r in results] == ["a.txt", "b.txt"]
    assert "kaboom" in results[1]["error"]


@pytest.mark.parametrize("executor", ["process", "thread"])
def test_two_workers_cancel_the_pending_files(tmp_path, spawn_child_env, executor):
    names = ("a.txt", "b.txt") + tuple(f"f{i:02d}.txt" for i in range(16))
    raw, files = _files(tmp_path, names)
    log = tmp_path / "calls"
    results = run_per_file(_fail_on_b, files, workers=2, raw_dir=raw, log=log, executor=executor)
    called = [x.name for x in log.iterdir()]
    assert len(called) < len(files)  # pending files were cancelled
    assert sorted(r["source_file"] for r in results) == sorted(called)  # in-flight kept
    assert first_failure(results)["source_file"] == "b.txt"
    assert all(r["ok"] for r in results if r["source_file"] != "b.txt")


def test_first_failure_ignores_skipped_results():
    rs = [
        {"source_file": "a", "skipped": True, "reason": "success_marker"},
        {"source_file": "b", "skipped": False, "ok": True},
    ]
    assert first_failure(rs) is None
    rs.append({"source_file": "c", "skipped": False, "ok": False, "error": "x"})
    assert first_failure(rs)["source_file"] == "c"


@pytest.mark.parametrize("workers", [1, 2])
def test_transient_error_twice_then_success_is_one_ok_result(tmp_path, spawn_child_env, workers):
    raw, files = _files(tmp_path, ("a.txt",))
    slept: list[float] = []
    results = run_per_file(
        _transient_twice,
        files,
        workers=workers,
        raw_dir=raw,
        counter=tmp_path / "count.txt",
        sleep=slept.append,
    )
    assert [r["ok"] for r in results] == [True]
    assert slept == [30, 60]


@pytest.mark.parametrize("workers", [1, 2])
def test_transient_error_after_the_last_retry_is_a_failed_result(
    tmp_path, spawn_child_env, workers
):
    raw, files = _files(tmp_path, ("a.txt",))
    slept: list[float] = []
    [r] = run_per_file(_always_transient, files, workers=workers, raw_dir=raw, sleep=slept.append)
    assert slept == [30, 60, 120]
    assert r["ok"] is False and r["skipped"] is False
    assert "TimeoutError" in r["error"]


def test_thread_mode_retries_in_threads(tmp_path):
    raw, files = _files(tmp_path, ("a.txt",))
    slept: list[float] = []
    results = run_per_file(
        _transient_twice,
        files,
        workers=2,
        executor="thread",
        raw_dir=raw,
        counter=tmp_path / "count.txt",
        sleep=slept.append,
    )
    assert [r["ok"] for r in results] == [True]
    assert slept == [30, 60]


def test_thread_mode_runs_in_this_process(tmp_path, monkeypatch):
    import episteme.data.parallel as par

    def _no_pool(*_a, **_k):
        raise AssertionError("process pool started in thread mode")

    monkeypatch.setattr(par, "ProcessPoolExecutor", _no_pool)
    raw, files = _files(tmp_path)
    results = run_per_file(_echo, files, workers=3, executor="thread", raw_dir=raw, tag="t")
    assert sorted(r["source_file"] for r in results) == ["a.txt", "b.txt", "c.txt", "d.txt"]
    assert {r["pid"] for r in results} == {os.getpid()}


def test_unknown_executor_is_rejected(tmp_path):
    raw, files = _files(tmp_path, ("a.txt",))
    with pytest.raises(ValueError):
        run_per_file(_echo, files, workers=2, executor="fork", raw_dir=raw, tag="t")


def test_broken_pool_fails_the_unit_without_sleeping(tmp_path, spawn_child_env):
    raw, files = _files(tmp_path, ("b.txt",))
    slept: list[float] = []
    [r] = run_per_file(_exit_on_b, files, workers=2, raw_dir=raw, sleep=slept.append)
    assert r["ok"] is False
    assert "BrokenProcessPool" in r["error"]
    assert slept == []


def test_broken_pool_after_a_retry_is_permanent(tmp_path, spawn_child_env):
    raw, files = _files(tmp_path, ("a.txt",))
    slept: list[float] = []
    counter = tmp_path / "count.txt"
    [r] = run_per_file(
        _transient_then_exit, files, workers=2, raw_dir=raw, counter=counter, sleep=slept.append
    )
    assert slept == [30]  # one retry for the transient error, none after the break
    assert counter.read_text(encoding="utf-8") == "2"
    assert r["ok"] is False and "BrokenProcessPool" in r["error"]


def test_dead_worker_fails_every_unit_in_flight(tmp_path, spawn_child_env):
    raw, files = _files(tmp_path)
    results = run_per_file(_exit_on_b_others_slow, files, workers=2, raw_dir=raw)
    assert results  # b at least
    for r in results:
        assert r["ok"] is False and r["skipped"] is False
        assert "BrokenProcessPool" in r["error"]


@pytest.mark.parametrize("executor", ["process", "thread"])
def test_a_stop_in_the_parent_cancels_the_pending_files(tmp_path, spawn_child_env, executor):
    names = ("a.txt",) + tuple(f"f{i:02d}.txt" for i in range(16))
    raw, files = _files(tmp_path, names)
    log = tmp_path / "calls"

    def _stop(_result):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_per_file(
            _fail_on_b, files, workers=2, raw_dir=raw, log=log, executor=executor, on_result=_stop
        )
    assert len(list(log.iterdir())) < len(files)  # queued files never started


def test_escaped_exception_is_logged_with_its_stack(tmp_path, caplog):
    raw, files = _files(tmp_path, ("b.txt",))
    with caplog.at_level("ERROR", logger="episteme.data.parallel"):
        run_per_file(_boom_on_b, files, workers=1, raw_dir=raw)
    assert any(r.exc_info and "kaboom" in str(r.exc_info[1]) for r in caplog.records)


def test_default_sleep_goes_through_the_module_seam(tmp_path, monkeypatch):
    import episteme.data.parallel as par

    calls: list[float] = []
    monkeypatch.setattr(par, "_SLEEP", calls.append)
    raw, files = _files(tmp_path, ("a.txt",))
    run_per_file(_always_transient, files, workers=1, raw_dir=raw)
    assert calls == [30, 60, 120]
