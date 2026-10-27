"""The Python stages write run status and progress (operating standard 6.6)."""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path

import pytest

from episteme.config import get_settings
from episteme.data import parallel, postgres_loader
from episteme.data.article_schema import empty_article_row, finalize_row
from episteme.data.staging_writer import write_rows
from episteme.ops import status, stop


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EPISTEME_RUN_ID", "run-t4")
    monkeypatch.delenv("EPISTEME_RUNNER_PID", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    status.set_current(status.NullStatus())


def _rec() -> dict:
    return status.RunStatus(status.run_dir("run-t4")).read()


def _beats() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "status-heartbeat" and t.is_alive()]


def test_guarded_stage_writes_running_status():
    seen = {}
    with stop.stage_guard(lambda: None, lock_stage="extract"):
        seen.update(_rec())
        assert isinstance(status.current(), status.RunStatus)
    assert seen["stage"] == "extract"
    assert seen["state"] == "running"
    assert "pid" in seen["pids"]["stage"]
    assert isinstance(status.current(), status.NullStatus)
    assert _beats() == []


def test_guard_keeps_the_runner_entry():
    status.RunStatus(status.run_dir("run-t4")).update(
        pids={"runner": {"pid": 1, "create_time": 2.0}}
    )
    with stop.stage_guard(lambda: None, lock_stage="extract"):
        pids = _rec()["pids"]
    assert pids["runner"] == {"pid": 1, "create_time": 2.0}
    assert "stage" in pids
    assert _rec()["state"] == "done"  # the guard always writes it; the runner writes after


def test_direct_run_ends_done():
    with stop.stage_guard(lambda: None, lock_stage="extract"):
        pass
    assert _rec()["state"] == "done"


def test_main_returning_1_ends_failed():
    with stop.stage_guard(lambda: None, lock_stage="extract") as g:
        g.exit_code(1)
    assert _rec()["state"] == "failed"


def test_main_returning_0_ends_done():
    with stop.stage_guard(lambda: None, lock_stage="extract") as g:
        assert g.exit_code(0) == 0
    assert _rec()["state"] == "done"


@pytest.mark.parametrize("rc", [130, 143])
def test_main_returning_stop_code_ends_stopped(rc):
    with stop.stage_guard(lambda: None, lock_stage="extract") as g:
        g.exit_code(rc)
    assert _rec()["state"] == "stopped"


def test_every_stage_guard_call_site_reports_its_exit_code():
    root = Path(stop.__file__).resolve().parents[1]
    bad = []
    for f in root.rglob("*.py"):
        if f.name == "stop.py":
            continue
        text = f.read_text(encoding="utf-8")
        calls = [
            ln
            for ln in text.splitlines()
            if "stage_guard(" in ln and not ln.lstrip().startswith(("#", "def "))
        ]
        if calls and ".exit_code(" not in text:
            bad.append(str(f))
    assert bad == []


def test_start_writes_started_at_and_last_progress():
    with stop.stage_guard(lambda: None, lock_stage="extract"):
        rec = _rec()
    assert rec["started_at"] and rec["last_progress"]


def test_status_failure_does_not_fail_the_stage(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no status")

    monkeypatch.setattr(status.RunStatus, "update", boom)
    ran = []
    with stop.stage_guard(lambda: None, lock_stage="extract"):
        ran.append(1)
    assert ran == [1]
    assert isinstance(status.current(), status.NullStatus)


def test_keyboard_interrupt_ends_stopped():
    with pytest.raises(SystemExit) as ei:
        with stop.stage_guard(lambda: None, lock_stage="extract"):
            raise KeyboardInterrupt
    assert ei.value.code in (130, 143)
    assert _rec()["state"] == "stopped"
    assert _beats() == []


def test_exception_ends_failed():
    with pytest.raises(RuntimeError):
        with stop.stage_guard(lambda: None, lock_stage="extract"):
            raise RuntimeError("boom")
    assert _rec()["state"] == "failed"


def _echo(path, **_kw):
    return {"source_file": Path(path).name, "ok": True}


def test_run_per_file_reports_progress():
    st = status.RunStatus(status.run_dir("run-t4"))
    status.set_current(st)
    files = [Path(f"f{i}.xml") for i in range(3)]
    parallel.run_per_file(_echo, files, workers=1)
    rec = st.read()
    assert rec["units_done"] == 3
    assert rec["units_total"] == 3
    assert rec["unit"] == "f2.xml"


class _Cur:
    rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return ("on", "on", "on", "on", True)  # the planner settings read

    @contextlib.contextmanager
    def copy(self, *a, **k):
        yield type("CP", (), {"write_row": lambda self, r: None})()


class _Conn:
    def cursor(self):
        return _Cur()


def _shard(tmp_path: Path, n: int) -> Path:
    rows = [
        finalize_row(
            {
                **empty_article_row(),
                "id": f"chembl:C{i}",
                "source": "chembl",
                "source_file": "F.db",
                "source_record_id": f"C{i}",
                "license": "CC BY-SA",
                "subset": "commercial",
            }
        )
        for i in range(n)
    ]
    info = write_rows(rows, tmp_path, source="chembl", source_file="F.db", prefer_parquet=True)
    return Path(info["paths"][0])


def test_load_source_file_reports_rows_per_batch(tmp_path, monkeypatch):
    st = status.RunStatus(status.run_dir("run-t4"))
    status.set_current(st)
    monkeypatch.setattr(postgres_loader.audit_trail, "record", lambda *a, **k: None)
    calls = []
    real = st.progress
    monkeypatch.setattr(st, "progress", lambda **kw: calls.append(kw) or real(**kw))
    base = postgres_loader._PROCESS_ROWS[0]
    postgres_loader.load_source_file(
        _Conn(), source="chembl", staging_path=_shard(tmp_path, 7), run_id="r", batch_size=3
    )
    assert [c["rows_done"] - base for c in calls] == [3, 6, 7]  # one call per batch
    st.heartbeat()  # flushes the progress held back by the throttle
    assert st.read()["rows_done"] == base + 7
    assert postgres_loader._PROCESS_ROWS[0] == base  # counted only after the caller commits
    postgres_loader.add_committed_rows(7)
    postgres_loader.reset_process_rows()
    assert postgres_loader._PROCESS_ROWS[0] == 0


def test_progress_is_throttled(tmp_path):
    st = status.RunStatus(tmp_path)
    now = [100.0]
    st._clock = lambda: now[0]
    st._t0 = 100.0
    st.progress(unit="a", units_done=1, units_total=10)  # first call writes
    now[0] = 100.5
    st.progress(unit="b", units_done=2, units_total=10)  # held
    assert st.read()["unit"] == "a"
    now[0] = 102.5
    st.progress(unit="c", units_done=3, units_total=10)  # new unit after 2 s writes
    assert st.read()["unit"] == "c"
    now[0] = 102.6
    st.progress(unit="d", units_done=4, units_total=10)  # held
    assert st.read()["unit"] == "c"
    st.update(state="done")  # an update flushes the held fields
    assert st.read()["unit"] == "d"
    now[0] = 102.7
    st.progress(unit="z", units_done=10, units_total=10)  # last unit always writes
    assert st.read()["units_done"] == 10


def test_long_statement_shows_while_the_body_runs():
    st = status.RunStatus(status.run_dir("run-t4"))
    status.set_current(st)
    with status.current().long_statement("CREATE INDEX x"):
        assert st.read()["long_statement"]["label"] == "CREATE INDEX x"
    assert st.read()["long_statement"] is None


@pytest.mark.parametrize("ended", ["done", "failed"])
def test_keep_stage_keeps_the_runner_stage_label(ended):
    """M10: bulk_indexes under `--bulk graph` keeps stage=graph; a done end
    keeps the end state the stage wrote."""
    status.RunStatus(status.run_dir("run-t4")).update(stage="graph", state=ended)
    with stop.stage_guard(lambda: None, lock_stage="load", keep_stage=True):
        during = _rec()
    assert during["stage"] == "graph" and during["state"] == "running"
    after = _rec()
    assert after["stage"] == "graph" and after["state"] == ended


def test_keep_stage_without_a_label_writes_its_own():
    with stop.stage_guard(lambda: None, lock_stage="load", keep_stage=True):
        assert _rec()["stage"] == "load"


def test_bulk_indexes_main_passes_keep_stage(monkeypatch):
    from episteme.data.db import bulk_indexes

    got = {}

    class _Stop(Exception):
        pass

    def fake_guard(ref, **kw):
        got.update(kw)
        raise _Stop

    monkeypatch.setattr(bulk_indexes, "stage_guard", fake_guard)
    monkeypatch.setattr(bulk_indexes.db_connection, "admin_dsn_from_settings", lambda: "x")
    with pytest.raises(_Stop):
        bulk_indexes.main(["drop", "--group", "pubmed_graph"])
    assert got == {
        "lock_stage": "load",
        "db_lock": True,
        "keep_stage": True,
        "argv": ["drop", "--group", "pubmed_graph"],
        "dsn": "x",
    }


def test_direct_run_writes_the_source():
    with stop.stage_guard(lambda: None, lock_stage="extract", source="pmc"):
        assert _rec()["source"] == "pmc"
    assert _rec()["source"] == "pmc"


def test_guard_clears_the_old_stage_progress_on_a_stage_change():
    status.RunStatus(status.run_dir("run-t4")).update(
        stage="extract", state="done", started_at="x", unit="u1", units_done=3, rows_done=5
    )
    with stop.stage_guard(lambda: None, lock_stage="load"):
        rec = _rec()
    assert rec["stage"] == "load"
    assert rec["unit"] is None and rec["units_done"] is None and rec["rows_done"] is None


def test_advisory_lock_connects_with_the_given_dsn(monkeypatch):
    import psycopg

    from episteme.ops import lock

    seen = []

    class _Conn:
        def execute(self, *a, **k):
            class _R:
                def fetchone(self):
                    return (True,)

            return _R()

        def close(self):
            pass

    def fake_connect(dsn, **kw):
        seen.append((dsn, kw.get("application_name")))
        return _Conn()

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    with lock.advisory_lock(dsn="ADMIN-DSN"):
        pass
    assert seen[0][0] == "ADMIN-DSN" and seen[0][1].endswith(":lock")
