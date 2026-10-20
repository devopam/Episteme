"""Run status files and the progress-based stall check (ops/status.py)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from episteme.ops import lock, status


def _iso(age_s: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat(timespec="seconds")


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_runs_root_is_inside_the_pytest_temp_root(tmp_path_factory):
    base = tmp_path_factory.getbasetemp().resolve()
    root = status.runs_root().resolve()
    assert base in root.parents


def test_update_merges_and_leaves_no_tmp(tmp_path):
    st = status.RunStatus(tmp_path / "r1")
    st.update(a=1)
    st.update(b=2)
    rec = st.read()
    assert rec["a"] == 1 and rec["b"] == 2 and "updated_at" in rec
    assert [p.name for p in (tmp_path / "r1").iterdir()] == ["status.json"]


def test_read_missing_is_empty(tmp_path):
    assert status.RunStatus(tmp_path / "none").read() == {}


def test_progress_rows_and_rate(tmp_path):
    st = status.RunStatus(tmp_path)
    time.sleep(0.01)
    rec = st.progress(rows_done=1000)
    assert rec["rows_done"] == 1000
    assert rec["rate_rows_per_s"] > 0
    assert rec["last_progress"] and rec["heartbeat"]


def test_progress_eta(tmp_path):
    st = status.RunStatus(tmp_path)
    time.sleep(0.01)
    rec = st.progress(unit="u", units_done=2, units_total=4)
    assert rec["unit"] == "u" and "eta_s" in rec


def test_long_statement_set_and_cleared(tmp_path):
    st = status.RunStatus(tmp_path)
    with st.long_statement("DELETE ..."):
        ls = st.read()["long_statement"]
        assert ls["label"] == "DELETE ..." and ls["since"]
    assert st.read()["long_statement"] is None
    with pytest.raises(RuntimeError):
        with st.long_statement("X"):
            raise RuntimeError("boom")
    assert st.read()["long_statement"] is None


def test_heartbeat_thread_does_not_touch_last_progress(tmp_path):
    st = status.RunStatus(tmp_path)
    st.update(last_progress="2000-01-01T00:00:00+00:00")
    stop = threading.Event()
    t = st.start_heartbeat(stop, interval=0.01)
    time.sleep(0.2)
    stop.set()
    t.join(timeout=2)
    assert not t.is_alive()
    rec = st.read()
    assert rec["heartbeat"]
    assert rec["last_progress"] == "2000-01-01T00:00:00+00:00"


def test_stalled_old_progress():
    rec = {"state": "running", "last_progress": _iso(660)}
    assert status.stalled(rec).startswith("stalled:")


def test_stalled_fresh_heartbeat_old_progress_is_still_stalled():
    rec = {"state": "running", "heartbeat": _iso(1), "last_progress": _iso(660)}
    assert status.stalled(rec).startswith("stalled:")


def test_stalled_long_statement_allowance():
    assert (
        status.stalled(
            {"state": "running", "last_progress": _iso(660), "long_statement": {"label": "x"}}
        )
        is None
    )
    reason = status.stalled(
        {"state": "running", "last_progress": _iso(3660), "long_statement": {"label": "x"}}
    )
    assert reason.startswith("stalled:")


def test_stalled_terminal_state_is_none():
    assert status.stalled({"state": "done", "last_progress": _iso(9999)}) is None


def test_stalled_dead_runner_is_crashed():
    rec = {
        "state": "running",
        "last_progress": _iso(1),
        "pids": {"runner": {"pid": _dead_pid(), "create_time": 1.0}},
    }
    assert status.stalled(rec).startswith("crashed:")


def test_stalled_live_runner_fresh_is_none():
    me = {"pid": os.getpid(), "create_time": lock.create_time(os.getpid())}
    assert (
        status.stalled({"state": "running", "last_progress": _iso(1), "pids": {"runner": me}})
        is None
    )


def test_cli_flow(capsys):
    assert (
        status.main(
            ["set", "--run-id", "R", "--source", "s", "--stage", "st", "--state", "running"]
        )
        == 0
    )
    assert status.main(["progress", "--run-id", "R", "--unit", "u1"]) == 0
    assert status.main(["--stalled"]) == 0
    status.RunStatus(status.run_dir("R")).update(last_progress=_iso(700))
    capsys.readouterr()
    assert status.main(["--stalled"]) == 1
    out = capsys.readouterr().out
    assert "R" in out and "stalled:" in out


def test_cli_set_running_records_runner_pid():
    status.main(
        [
            "set",
            "--run-id",
            "R2",
            "--source",
            "s",
            "--stage",
            "a",
            "--state",
            "running",
            "--runner-pid",
            str(os.getpid()),
        ]
    )
    rec = status.RunStatus(status.run_dir("R2")).read()
    assert rec["pids"]["runner"]["pid"] == os.getpid()
    assert rec["started_at"] and rec["last_progress"]


def test_cli_long_begin_end():
    status.main(["set", "--run-id", "R3", "--source", "s", "--stage", "a", "--state", "running"])
    status.main(["long-begin", "--run-id", "R3", "--label", "VACUUM"])
    assert status.RunStatus(status.run_dir("R3")).read()["long_statement"]["label"] == "VACUUM"
    status.main(["long-end", "--run-id", "R3"])
    assert status.RunStatus(status.run_dir("R3")).read()["long_statement"] is None


def test_cli_run_dir_and_queue_dir(capsys):
    status.main(["run-dir", "--run-id", "R"])
    assert capsys.readouterr().out.strip() == str(status.runs_root() / "R")
    status.main(["queue-dir", "--queue-id", "Q1"])
    out = capsys.readouterr().out.strip()
    assert out.replace("\\", "/").endswith("_ops/queue/Q1")


def test_cli_stalled_skips_damaged_file(capsys):
    d = status.run_dir("BAD")
    d.mkdir(parents=True)
    (d / "status.json").write_text("{not json", encoding="utf-8")
    status.main(["set", "--run-id", "OK", "--source", "s", "--stage", "a", "--state", "running"])
    assert status.main(["--stalled"]) == 0
    assert "BAD" in capsys.readouterr().err


def test_update_retries_a_transient_permission_error(tmp_path, monkeypatch):
    real = os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("held")
        return real(a, b)

    monkeypatch.setattr(os, "replace", flaky)
    st = status.RunStatus(tmp_path)
    st.update(a=1)
    assert st.read()["a"] == 1


def test_update_never_raises_when_replace_always_fails(tmp_path, monkeypatch, caplog):
    def broken(a, b):
        raise PermissionError("held")

    st = status.RunStatus(tmp_path)
    monkeypatch.setattr(os, "replace", broken)
    with caplog.at_level("WARNING"):
        rec = st.progress(rows_done=5)
    assert rec["rows_done"] == 5
    assert "could not write" in caplog.text
    assert [p.name for p in tmp_path.iterdir()] == []
    with st.long_statement("X"):  # the stage goes on
        pass
    with pytest.raises(RuntimeError):  # the finally never hides the body error
        with st.long_statement("X"):
            raise RuntimeError("boom")
    args = ["set", "--run-id", "Z", "--source", "s", "--stage", "a", "--state", "done"]
    assert status.main(args) == 0


def test_read_retries_then_gives_empty(tmp_path, monkeypatch, caplog):
    st = status.RunStatus(tmp_path / "runs" / "r1")
    st.update(a=1)
    real = status.Path.read_text
    calls = {"n": 0}

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("held")
        return real(self, *a, **k)

    monkeypatch.setattr(status.Path, "read_text", flaky)
    assert st.read()["a"] == 1

    def broken(self, *a, **k):
        raise PermissionError("held")

    monkeypatch.setattr(status.Path, "read_text", broken)
    with caplog.at_level("WARNING"):
        assert st.read() == {}
    assert "could not read" in caplog.text
    assert status.list_runs(tmp_path / "runs") == [("r1", {})]


def test_heartbeat_keeps_fields_written_by_another_writer(tmp_path):
    st = status.RunStatus(tmp_path)
    st.update(a=1)
    other = status.RunStatus(tmp_path)  # a CLI process
    other.update(long_statement={"label": "L", "since": "x"})
    st.heartbeat()
    rec = st.read()
    assert rec["long_statement"]["label"] == "L" and rec["heartbeat"] and rec["a"] == 1


def test_null_status_heartbeat_returns_a_thread():
    t = status.NullStatus().start_heartbeat(threading.Event())
    t.join(timeout=1)
    assert not t.is_alive()


def test_cli_progress_on_unknown_run_exits_2(capsys):
    assert status.main(["progress", "--run-id", "NOPE"]) == 2
    assert "no status file" in capsys.readouterr().err
    assert not status.run_dir("NOPE").exists()


def test_stalled_naive_timestamp_counts_as_utc():
    naive = (datetime.now(timezone.utc) - timedelta(seconds=700)).replace(tzinfo=None)
    rec = {"state": "running", "last_progress": naive.isoformat(timespec="seconds")}
    assert status.stalled(rec).startswith("stalled:")


def test_set_keeps_started_at_when_stage_unchanged():
    args = ["set", "--run-id", "K", "--source", "s", "--stage", "a", "--state", "running"]
    status.main(args)
    first = status.RunStatus(status.run_dir("K")).read()["started_at"]
    time.sleep(1.1)
    status.main(args)
    assert status.RunStatus(status.run_dir("K")).read()["started_at"] == first


def test_queue_dir_is_under_processed_root(capsys):
    from episteme.config import get_settings

    status.main(["queue-dir", "--queue-id", "Q9"])
    out = capsys.readouterr().out.strip()
    assert out == str(get_settings().processed_root / "_ops" / "queue" / "Q9")


def test_two_threads_update_lose_no_fields(tmp_path):
    st = status.RunStatus(tmp_path)

    def work(prefix):
        for i in range(30):
            st.update(**{f"{prefix}{i}": i})

    ts = [threading.Thread(target=work, args=(p,)) for p in "ab"]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    rec = st.read()
    assert all(f"{p}{i}" in rec for p in "ab" for i in range(30))


def test_failed_read_never_overwrites_an_existing_record(tmp_path, monkeypatch):
    st = status.RunStatus(tmp_path)
    st.update(source="s", stage="a", pids={"runner": {"pid": 1}})
    before = st.path.read_bytes()

    def broken(self, *a, **k):
        raise PermissionError("held")

    monkeypatch.setattr(status.Path, "read_text", broken)
    rec = st.update(x=1)
    assert rec["x"] == 1
    st.heartbeat()
    monkeypatch.undo()
    assert st.path.read_bytes() == before


def test_damaged_json_is_repaired_with_the_new_fields(tmp_path, caplog):
    st = status.RunStatus(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    st.path.write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        st.update(a=1)
    assert st.read()["a"] == 1
    assert "repair" in caplog.text
    st.path.write_text("[1, 2]", encoding="utf-8")
    st.update(b=2)
    assert st.read()["b"] == 2


def test_status_warnings_are_rate_limited(tmp_path, monkeypatch, caplog):
    clock = {"t": 1000.0}
    monkeypatch.setattr(status.time, "monotonic", lambda: clock["t"])

    def broken(a, b):
        raise PermissionError("held")

    monkeypatch.setattr(os, "replace", broken)
    st = status.RunStatus(tmp_path)
    with caplog.at_level("WARNING"):
        for _ in range(3):
            st.update(a=1)
        assert caplog.text.count("could not write") == 1
        clock["t"] += 61
        st.update(a=1)
    assert caplog.text.count("could not write") == 2


def test_stalled_direct_run_with_a_dead_stage_is_crashed():
    """M3: with no runner entry the stage process is checked the same way."""
    rec = {
        "state": "running",
        "last_progress": _iso(1),
        "pids": {"stage": {"pid": _dead_pid(), "create_time": 1.0}},
    }
    assert status.stalled(rec) == "crashed: the stage process is gone"


def test_stalled_direct_run_with_a_live_stage_is_none():
    me = {"pid": os.getpid(), "create_time": lock.create_time(os.getpid())}
    rec = {"state": "running", "last_progress": _iso(1), "pids": {"stage": me}}
    assert status.stalled(rec) is None


def test_stalled_runner_entry_wins_over_a_dead_stage():
    """Under the runner a stage process ends between stages; only the runner counts."""
    me = {"pid": os.getpid(), "create_time": lock.create_time(os.getpid())}
    rec = {
        "state": "running",
        "last_progress": _iso(1),
        "pids": {"runner": me, "stage": {"pid": _dead_pid(), "create_time": 1.0}},
    }
    assert status.stalled(rec) is None
