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
    me = {"pid": os.getpid(), "create_time": lock._create_time(os.getpid())}
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
