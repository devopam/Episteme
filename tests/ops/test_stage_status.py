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
    assert _rec()["state"] == "running"  # the runner writes the end state


def test_direct_run_ends_done():
    with stop.stage_guard(lambda: None, lock_stage="extract"):
        pass
    assert _rec()["state"] == "done"


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
    assert st.read()["rows_done"] == base + 7


def test_long_statement_shows_while_the_body_runs():
    st = status.RunStatus(status.run_dir("run-t4"))
    status.set_current(st)
    with status.current().long_statement("CREATE INDEX x"):
        assert st.read()["long_statement"]["label"] == "CREATE INDEX x"
    assert st.read()["long_statement"] is None
