"""run_per_file: in-process for one worker, spawned processes above one.

Every helper here is module-level: a spawned child imports this module by name.
Every multi-worker test takes ``spawn_child_env`` (see tests/conftest.py)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from episteme.data.checkpoint_markers import input_key
from episteme.data.parallel import run_per_file


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
    results = run_per_file(_boom_on_b, files, workers=3, raw_dir=raw)
    by = {r["source_file"]: r for r in results}
    assert set(by) == {"a.txt", "b.txt", "c.txt", "d.txt"}
    assert by["b.txt"]["ok"] is False
    assert by["b.txt"]["skipped"] is False
    assert by["b.txt"]["error"].startswith("worker: ")
    assert "kaboom" in by["b.txt"]["error"]
    assert all(by[n]["ok"] for n in ("a.txt", "c.txt", "d.txt"))


def test_dead_worker_fails_the_remaining_files_without_raising(tmp_path, spawn_child_env):
    raw, files = _files(tmp_path)
    results = run_per_file(_exit_on_b, files, workers=2, raw_dir=raw)
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
