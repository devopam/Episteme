"""The gate command line (ops/gate.py ``main``)."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from episteme.ops import gate, gate_migrations, gate_report

SRC = "bookshelf"
RULES = {"extract": {"min_rows": 3, "max_duplicate_ids": 0}, "load": {"max_orphan_bodies": 0}}


@pytest.fixture
def rules_dir(tmp_path, monkeypatch):
    d = tmp_path / "rules"
    d.mkdir()
    monkeypatch.setattr(gate, "RULES_DIR", d)
    (d / f"{SRC}.json").write_text(json.dumps(RULES), encoding="utf-8")
    return d


@pytest.fixture
def fake_hash(monkeypatch):
    state = {"h": "hash-1"}
    monkeypatch.setattr(gate, "code_hash", lambda source, stage, **kw: state["h"])
    return state


# --- run --------------------------------------------------------------------


def test_run_returns_child_exit_code_and_peak_rss(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "POLL_S", 0.1)
    rec = tmp_path / "rec.json"
    code = "import time; b = bytearray(b'x' * 50_000_000); time.sleep(1.0); raise SystemExit(7)"
    rc = gate.main(["run", "--record", str(rec), "--", sys.executable, "-c", code])
    assert rc == 7
    data = json.loads(rec.read_text(encoding="utf-8"))
    assert data["rc"] == 7
    assert data["peak_rss_mb"] > 40
    assert data["elapsed_s"] >= 1.0


def test_run_zero_exit(tmp_path):
    rec = tmp_path / "sub" / "rec.json"
    assert gate.main(["run", "--record", str(rec), "--", sys.executable, "-c", "pass"]) == 0
    assert json.loads(rec.read_text(encoding="utf-8"))["rc"] == 0


def test_run_without_command_is_a_usage_error(tmp_path):
    assert gate.main(["run", "--record", str(tmp_path / "r.json"), "--"]) == 2


def test_run_unstartable_command(tmp_path):
    assert gate.main(["run", "--record", str(tmp_path / "r.json"), "--", "no-such-cmd-xyz"]) == 127


def test_run_sums_rss_over_descendants(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "POLL_S", 0.1)
    rec = tmp_path / "rec.json"
    code = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', "
        "\"import time; b = bytearray(b'x' * 60_000_000); time.sleep(1.5)\"])\n"
        "time.sleep(1.5); p.wait()\n"
    )
    assert gate.main(["run", "--record", str(rec), "--", sys.executable, "-c", code]) == 0
    assert json.loads(rec.read_text(encoding="utf-8"))["peak_rss_mb"] > 55


# --- paths ------------------------------------------------------------------


def test_paths_prints_only_the_processed_root(tmp_path, monkeypatch, capsys):
    from episteme import config

    processed = tmp_path / "proc"
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(processed))
    config.get_settings.cache_clear()
    try:
        assert gate.main(["paths"]) == 0
    finally:
        config.get_settings.cache_clear()
    out = capsys.readouterr()
    assert out.out == f"{processed}\n"


# --- prepare ----------------------------------------------------------------


def test_prepare_prints_sample_root_and_writes_slice(tmp_path, capsys):
    raw = tmp_path / "raw"
    pkg = raw / "bookshelf" / "packages" / "b0"
    pkg.mkdir(parents=True)
    (pkg / "book000.tar.gz").write_bytes(b"x" * 100)
    processed = tmp_path / "proc"
    sample = processed / "_sample" / SRC
    rc = gate.main(
        [
            "prepare",
            "--source",
            SRC,
            "--raw-root",
            str(raw),
            "--sample-root",
            str(sample),
            "--processed-root",
            str(processed),
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out == f"{sample}\n"
    assert json.loads((sample / "slice.json").read_text(encoding="utf-8"))["files"]


def test_prepare_refuses_a_sample_root_outside_the_sample_base(tmp_path):
    rc = gate.main(
        [
            "prepare",
            "--source",
            SRC,
            "--raw-root",
            str(tmp_path / "raw"),
            "--sample-root",
            str(tmp_path / "elsewhere"),
            "--processed-root",
            str(tmp_path / "proc"),
        ]
    )
    assert rc == 2


# --- report -----------------------------------------------------------------


def _sample(tmp_path: Path, rows: int, *, dup: bool = False) -> tuple[Path, Path]:
    sample = tmp_path / "proc" / "_sample" / SRC
    raw = sample / "01_raw" / "bookshelf"
    raw.mkdir(parents=True)
    (raw / "book1.tar.gz").write_bytes(b"x" * 1000)
    staging = sample / "02_processed" / "staging" / SRC
    staging.mkdir(parents=True)
    lines = []
    for i in range(rows):
        rid = "same" if dup and i < 2 else f"id{i}"
        lines.append(
            json.dumps(
                {
                    "id": rid,
                    "source": SRC,
                    "text": "hello world " * 5,
                    "license": "CC BY",
                    "extract_status": "ok",
                }
            )
        )
    (staging / "book1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (sample / "slice.json").write_text(
        json.dumps({"files": ["book1.tar.gz"], "bytes": 1000}), encoding="utf-8"
    )
    rec = tmp_path / "rec.json"
    rec.write_text(json.dumps(_record()), encoding="utf-8")
    return sample, rec


def _record(**over) -> dict:
    """A run record of `gate run --source --stage`: it holds the hashes taken at
    the start of the run (F7). The tests use the ``fake_hash`` fixture."""
    rec = {
        "elapsed_s": 2.0,
        "rc": 0,
        "peak_rss_mb": 123.0,
        "code_hash": gate.code_hash(SRC, "extract"),
        "rules_hash": gate.rules_hash(SRC),
    }
    rec.update(over)
    return rec


def _report_args(sample: Path, rec: Path, pass_root: Path, stage: str = "extract") -> list[str]:
    return [
        "report",
        "--source",
        SRC,
        "--stage",
        stage,
        "--sample-root",
        str(sample),
        "--pass-root",
        str(pass_root),
        "--record",
        str(rec),
    ]


def test_report_writes_pass_under_pass_root_only(tmp_path, rules_dir, fake_hash):
    sample, rec = _sample(tmp_path, rows=5)
    pass_root = tmp_path / "prod"
    assert gate.main(_report_args(sample, rec, pass_root)) == 0
    assert gate.pass_path(SRC, "extract", pass_root).is_file()
    assert (sample / "gate_report.json").is_file()
    assert (sample / "gate_report.md").is_file()
    assert not list(sample.rglob("*.pass"))
    report = json.loads((sample / "gate_report.json").read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["measures"]["peak_rss_mb"] == 123.0


def test_report_with_failing_rule_writes_no_pass(tmp_path, rules_dir, fake_hash, capsys):
    sample, rec = _sample(tmp_path, rows=2)
    pass_root = tmp_path / "prod"
    assert gate.main(_report_args(sample, rec, pass_root)) == 1
    assert "min_rows" in capsys.readouterr().out
    assert not pass_root.exists()
    assert json.loads((sample / "gate_report.json").read_text(encoding="utf-8"))["passed"] is False


def test_report_fails_on_duplicate_ids(tmp_path, rules_dir, fake_hash):
    sample, rec = _sample(tmp_path, rows=5, dup=True)
    assert gate.main(_report_args(sample, rec, tmp_path / "prod")) == 1


def test_report_fails_when_the_run_failed(tmp_path, rules_dir, fake_hash, capsys):
    sample, rec = _sample(tmp_path, rows=5)
    rec.write_text(json.dumps(_record(rc=3)), encoding="utf-8")
    assert gate.main(_report_args(sample, rec, tmp_path / "prod")) == 1
    assert "rc 3" in capsys.readouterr().out


def test_report_ungated_stage_is_a_usage_error(tmp_path, rules_dir, fake_hash):
    sample, rec = _sample(tmp_path, rows=5)
    (rules_dir / f"{SRC}.json").write_text(json.dumps({"load": {}}), encoding="utf-8")
    assert gate.main(_report_args(sample, rec, tmp_path / "prod")) == 2


class _Cur:
    def __init__(self, rows_by_query):
        self._rows = rows_by_query
        self._last = None
        self.all_params = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.all_params.append(params)
        self._last = sql

    def fetchone(self):
        return (self._rows["orphan"] if "article_body" in self._last else self._rows["loaded"],)

    def fetchall(self):
        return [(n,) for n in self._rows["migrations"]]


class _Conn:
    def __init__(self, **rows):
        self.rows = rows
        self.seen = []

    def cursor(self):
        cur = _Cur(self.rows)
        self.seen.append(cur)
        return cur


def _fake_connect(monkeypatch, conn):
    @contextlib.contextmanager
    def connect():
        yield conn

    monkeypatch.setattr(gate, "_connect", connect)


def test_report_load_stage_uses_the_database(tmp_path, rules_dir, fake_hash, monkeypatch):
    sample, rec = _sample(tmp_path, rows=5)
    conn = _Conn(loaded=5, orphan=0)
    _fake_connect(monkeypatch, conn)
    pass_root = tmp_path / "prod"
    assert gate.main(_report_args(sample, rec, pass_root, "load")) == 0
    assert gate.pass_path(SRC, "load", pass_root).is_file()
    # the sample's source_files use the input key of the slice file
    assert any((SRC, ["book1.tar.gz"]) in c.all_params for c in conn.seen)


def test_report_load_stage_fails_on_orphans(tmp_path, rules_dir, fake_hash, monkeypatch):
    sample, rec = _sample(tmp_path, rows=5)
    _fake_connect(monkeypatch, _Conn(loaded=5, orphan=2))
    assert gate.main(_report_args(sample, rec, tmp_path / "prod", "load")) == 1
    assert not (tmp_path / "prod").exists()


# --- check ------------------------------------------------------------------


def test_check_not_gated(tmp_path, rules_dir, fake_hash, capsys):
    rc = gate.main(
        ["check", "--source", "nope", "--stage", "extract", "--pass-root", str(tmp_path)]
    )
    assert rc == 0
    assert capsys.readouterr().out.strip() == "not gated"


def test_check_all_reasons(tmp_path, rules_dir, fake_hash, capsys):
    args = ["check", "--source", SRC, "--stage", "extract", "--pass-root", str(tmp_path)]

    assert gate.main(args) == 1
    assert capsys.readouterr().out.strip() == "no pass"

    gate.write_pass(SRC, "extract", tmp_path, tmp_path / "r.json")
    assert gate.main(args) == 0
    assert capsys.readouterr().out.strip() == "ok"

    fake_hash["h"] = "hash-2"
    assert gate.main(args) == 1
    assert capsys.readouterr().out.strip() == "code changed"

    fake_hash["h"] = "hash-1"
    (rules_dir / f"{SRC}.json").write_text(json.dumps({"extract": {"min_rows": 9}}))
    assert gate.main(args) == 1
    assert capsys.readouterr().out.strip() == "rules changed"


# --- migrations-check -------------------------------------------------------


def test_every_migration_file_has_markers():
    files = {p.name for p in gate._MIGRATIONS_DIR.glob("*.sql")}
    assert files
    deferred = set(gate_migrations.DEFERRED_MIGRATIONS)
    assert deferred <= files
    assert all(gate_migrations.DEFERRED_MIGRATIONS.values())
    assert set(gate_migrations.MIGRATION_MARKERS) <= files
    assert set(gate_migrations.MIGRATION_MARKERS) | deferred == files
    for markers in gate_migrations.MIGRATION_MARKERS.values():
        assert 1 <= len(markers) <= 3
        assert all(m.split(":")[0] in ("table", "column", "index") for m in markers)


class _MarkerCur:
    def __init__(self, present):
        self.present = present
        self._ans = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        kind = "table" if "to_regclass" in sql else "column" if "columns" in sql else "index"
        self._ans = f"{kind}:{'.'.join(params)}" in self.present

    def fetchone(self):
        return (self._ans,)


class _MarkerConn:
    def __init__(self, present):
        self.present = present

    def cursor(self):
        return _MarkerCur(self.present)


def test_migrations_check_lists_missing_objects(tmp_path, monkeypatch, capsys):
    mig = tmp_path / "mig"
    mig.mkdir()
    for n in ("0001_a.sql", "0002_b.sql", "0003_c.sql"):
        (mig / n).write_text("select 1;")
    monkeypatch.setattr(gate, "_MIGRATIONS_DIR", mig)
    monkeypatch.setattr(
        gate_migrations,
        "MIGRATION_MARKERS",
        {
            "0001_a.sql": ["table:s.a"],
            "0002_b.sql": ["table:s.b", "column:s.b.c", "index:s.b_idx"],
        },
    )
    conn = _MarkerConn({"table:s.a", "table:s.b", "column:s.b.c"})
    _fake_connect(monkeypatch, conn)
    assert gate.main(["migrations-check"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "0002_b.sql: missing index:s.b_idx",
        "0003_c.sql: missing no markers defined",
    ]

    conn.present |= {"index:s.b_idx"}
    monkeypatch.setitem(gate_migrations.MIGRATION_MARKERS, "0003_c.sql", ["table:s.c"])
    conn.present |= {"table:s.c"}
    assert gate.main(["migrations-check"]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.pg
def test_migrations_check_against_episteme_test(capsys):
    from episteme.data.db.connection import connection

    with connection() as conn, conn.cursor() as cur:
        cur.execute("select current_database(), current_setting('server_version')")
        assert cur.fetchone() == ("episteme_test", "19beta3")
    rc = gate.main(["migrations-check"])
    out = capsys.readouterr().out
    print(out)
    # Before the rollout the check names the missing migrations; after it, none.
    assert rc == (1 if out else 0)
    assert all(line.split(":")[0].endswith(".sql") for line in out.splitlines())


# --- module entry -----------------------------------------------------------


def test_module_entry_paths_prints_one_line(tmp_path):
    env = {**os.environ, "EPISTEME_PROCESSED_ROOT": str(tmp_path / "p")}
    proc = subprocess.run(
        [sys.executable, "-m", "episteme.ops.gate", "paths"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == str(tmp_path / "p")


def test_run_stops_the_child_on_an_unexpected_error(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "POLL_S", 0.1)
    started = []
    real_popen = subprocess.Popen

    def popen(*a, **k):
        p = real_popen(*a, **k)
        started.append(p)
        return p

    def boom(_proc):
        raise RuntimeError("poll failed")

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(gate, "_tree_rss", boom)
    code = "import time; time.sleep(60)"
    with pytest.raises(RuntimeError):
        gate.main(["run", "--record", str(tmp_path / "r.json"), "--", sys.executable, "-c", code])
    assert started[0].poll() is not None


def test_deferred_migration_is_skipped_and_reported_on_stderr(tmp_path, monkeypatch, capsys):
    mig = tmp_path / "mig"
    mig.mkdir()
    for n in ("0001_a.sql", "0002_b.sql"):
        (mig / n).write_text("select 1;")
    monkeypatch.setattr(gate, "_MIGRATIONS_DIR", mig)
    monkeypatch.setattr(
        gate_migrations,
        "MIGRATION_MARKERS",
        {"0001_a.sql": ["table:s.a"], "0002_b.sql": ["table:s.b"]},
    )
    monkeypatch.setattr(gate_migrations, "DEFERRED_MIGRATIONS", {"0001_a.sql": "no pgvector"})
    _fake_connect(monkeypatch, _MarkerConn({"table:s.b"}))
    assert gate.main(["migrations-check"]) == 0
    out = capsys.readouterr()
    assert out.out == ""
    assert "0001_a.sql: deferred (no pgvector)" in out.err

    _fake_connect(monkeypatch, _MarkerConn(set()))
    assert gate.main(["migrations-check"]) == 1
    assert capsys.readouterr().out.splitlines() == ["0002_b.sql: missing table:s.b"]


# --- fix round: R10 exit codes, a failed report drops the old pass -----------


def _prepare_raising(monkeypatch, tmp_path, exc: Exception) -> int:
    from episteme.ops import gate_slice

    def boom(*_a, **_k):
        raise exc

    monkeypatch.setattr(gate_slice, "prepare", boom)
    return gate.main(
        [
            "prepare",
            "--source",
            SRC,
            "--raw-root",
            str(tmp_path / "raw"),
            "--sample-root",
            str(tmp_path / "proc" / "_sample" / SRC),
            "--processed-root",
            str(tmp_path / "proc"),
        ]
    )


def test_prepare_exits_2_for_a_slice_refusal(tmp_path, monkeypatch):
    from episteme.ops import gate_slice

    assert _prepare_raising(monkeypatch, tmp_path, gate_slice.SliceError("over the cap")) == 2
    assert _prepare_raising(monkeypatch, tmp_path, FileNotFoundError("missing")) == 2


def test_prepare_exits_1_for_an_io_error(tmp_path, monkeypatch, capsys):
    assert _prepare_raising(monkeypatch, tmp_path, PermissionError("denied")) == 1
    assert "denied" in capsys.readouterr().err


def test_a_failed_report_deletes_the_old_pass(tmp_path, rules_dir, fake_hash):
    sample, rec = _sample(tmp_path, rows=5)
    pass_root = tmp_path / "prod"
    assert gate.main(_report_args(sample, rec, pass_root)) == 0
    assert gate.pass_path(SRC, "extract", pass_root).is_file()
    sample, rec = _sample(tmp_path / "again", rows=2)
    assert gate.main(_report_args(sample, rec, pass_root)) == 1
    assert not gate.pass_path(SRC, "extract", pass_root).exists()


# --- final fix wave: F1 distinct ids, F2 yield units, F7 hash once ------------


def _measures(sample: Path) -> dict:
    return json.loads((sample / "gate_report.json").read_text(encoding="utf-8"))["measures"]


def test_report_load_counts_a_cross_shard_duplicate_once(
    tmp_path, rules_dir, fake_hash, monkeypatch
):
    """F1: an id re-issued in a second shard (another source_file) is loaded once:
    the loader's id-collision guard deletes the older row. 6 staging rows, 5 ids."""
    (rules_dir / f"{SRC}.json").write_text(
        json.dumps({"load": {"max_orphan_bodies": 0, "rows_loaded_equals_staging": True}}),
        encoding="utf-8",
    )
    sample, rec = _sample(tmp_path, rows=5)
    rec.write_text(json.dumps(_record()), encoding="utf-8")
    staging = sample / "02_processed" / "staging" / SRC
    (staging / "book2.jsonl").write_text(
        json.dumps({"id": "id0", "source": SRC, "text": "again " * 5, "license": "CC BY"}) + "\n",
        encoding="utf-8",
    )
    _fake_connect(monkeypatch, _Conn(loaded=5, orphan=0))
    pass_root = tmp_path / "prod"
    assert gate.main(_report_args(sample, rec, pass_root, "load")) == 0
    m = _measures(sample)
    assert m["rows"] == 6 and m["distinct_ids"] == 5
    assert m["rows_loaded_equals_staging"] is True
    assert gate.pass_path(SRC, "load", pass_root).is_file()


def _with_sidecar(sample: Path, *, units: bool) -> None:
    raw = sample / "01_raw" / "bookshelf"
    (raw / "book1.filelist.csv").write_bytes(b"y" * 500)
    meta = {"files": ["book1.tar.gz", "book1.filelist.csv"], "bytes": 1500}
    if units:
        meta["inputs"] = ["book1.tar.gz"]
    (sample / "slice.json").write_text(json.dumps(meta), encoding="utf-8")


def test_report_yield_counts_the_discovery_units_only(tmp_path, rules_dir, fake_hash):
    """F2: a sidecar file (.filelist.csv) is in the slice but is no unit of work."""
    sample, rec = _sample(tmp_path, rows=5)
    _with_sidecar(sample, units=True)
    assert gate.main(_report_args(sample, rec, tmp_path / "prod")) == 0
    m = _measures(sample)
    assert m["inputs"] == 1 and m["input_bytes"] == 1000
    assert m["rows_per_input"] == 5.0


def test_report_yield_falls_back_to_all_files_for_an_old_slice_json(tmp_path, rules_dir, fake_hash):
    sample, rec = _sample(tmp_path, rows=5)
    _with_sidecar(sample, units=False)
    assert gate.main(_report_args(sample, rec, tmp_path / "prod")) == 0
    m = _measures(sample)
    assert m["inputs"] == 2 and m["input_bytes"] == 1500


def test_run_records_the_hashes_at_its_start(tmp_path, rules_dir, fake_hash):
    rec = tmp_path / "rec.json"
    rc = gate.main(
        [
            "run",
            "--record",
            str(rec),
            "--source",
            SRC,
            "--stage",
            "extract",
            "--",
            sys.executable,
            "-c",
            "pass",
        ]
    )
    assert rc == 0
    data = json.loads(rec.read_text(encoding="utf-8"))
    assert data["code_hash"] == "hash-1"
    assert data["rules_hash"] == gate.rules_hash(SRC)


def test_report_refuses_a_pass_when_the_code_changed_during_the_run(
    tmp_path, rules_dir, fake_hash, capsys
):
    """F7: the stage code changes between `gate run` and `gate report`."""
    sample, rec = _sample(tmp_path, rows=5)
    pass_root = tmp_path / "prod"
    assert gate.main(_report_args(sample, rec, pass_root)) == 0
    fake_hash["h"] = "hash-2"
    assert gate.main(_report_args(sample, rec, pass_root)) == 1
    assert "changed during the sample run" in capsys.readouterr().out
    assert not gate.pass_path(SRC, "extract", pass_root).exists()


def test_report_refuses_a_pass_when_the_rules_changed_during_the_run(
    tmp_path, rules_dir, fake_hash, capsys
):
    sample, rec = _sample(tmp_path, rows=5)
    (rules_dir / f"{SRC}.json").write_text(
        json.dumps({"extract": {"min_rows": 4}}), encoding="utf-8"
    )
    assert gate.main(_report_args(sample, rec, tmp_path / "prod")) == 1
    assert "rules changed during the sample run" in capsys.readouterr().out


def test_the_pass_carries_the_hashes_of_the_run_start(tmp_path, rules_dir, fake_hash):
    sample, rec = _sample(tmp_path, rows=5)
    rec.write_text(json.dumps(_record(code_hash="hash-1")), encoding="utf-8")
    pass_root = tmp_path / "prod"
    assert gate.main(_report_args(sample, rec, pass_root)) == 0
    data = json.loads(gate.pass_path(SRC, "extract", pass_root).read_text(encoding="utf-8"))
    assert data["code_hash"] == "hash-1"
    assert data["rules_hash"] == gate.rules_hash(SRC)


def test_report_without_start_hashes_fails_closed(tmp_path, rules_dir, fake_hash, capsys):
    sample, rec = _sample(tmp_path, rows=5)
    rec.write_text(json.dumps({"elapsed_s": 1.0, "rc": 0, "peak_rss_mb": 1.0}), encoding="utf-8")
    assert gate.main(_report_args(sample, rec, tmp_path / "prod")) == 1
    assert "no code hash" in capsys.readouterr().out


def _sigterm_once_started(monkeypatch):
    """Popen spy; a thread sends SIGTERM (as a Python-level signal) to this
    process once the child runs, as the runner's forwarded stop does on POSIX."""
    import _thread
    import signal
    import threading
    import time

    started: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def popen(*a, **k):
        p = real_popen(*a, **k)
        started.append(p)
        return p

    def send():
        deadline = time.monotonic() + 30
        while not started and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        _thread.interrupt_main(signal.SIGTERM)

    monkeypatch.setattr(subprocess, "Popen", popen)
    threading.Thread(target=send, daemon=True).start()
    return started


def test_run_sigterm_records_143_and_lets_the_stage_end(tmp_path, monkeypatch):
    # The stage got the same SIGTERM: gate run waits for it, then writes its record.
    import signal

    monkeypatch.setattr(gate, "POLL_S", 0.1)
    monkeypatch.setattr(gate, "STOP_GRACE_S", 20.0)
    stopped = []
    monkeypatch.setattr(gate, "_stop_tree", lambda proc, child: stopped.append(1))
    before = signal.getsignal(signal.SIGTERM)
    started = _sigterm_once_started(monkeypatch)
    rec = tmp_path / "rec.json"
    code = "import time; time.sleep(2.0)"
    rc = gate.main(["run", "--record", str(rec), "--", sys.executable, "-c", code])
    assert rc == 143
    data = json.loads(rec.read_text(encoding="utf-8"))
    assert data["rc"] == 143 and data["interrupted"] is True
    assert started[0].returncode == 0  # it ended on its own
    assert stopped == []
    assert signal.getsignal(signal.SIGTERM) is before


def test_run_sigterm_ends_a_stage_that_does_not_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "POLL_S", 0.1)
    monkeypatch.setattr(gate, "STOP_GRACE_S", 0.5)
    started = _sigterm_once_started(monkeypatch)
    rec = tmp_path / "rec.json"
    code = "import time; time.sleep(60)"
    rc = gate.main(["run", "--record", str(rec), "--", sys.executable, "-c", code])
    assert rc == 143
    assert json.loads(rec.read_text(encoding="utf-8"))["rc"] == 143
    assert started[0].poll() is not None


def test_load_measures_use_staging_source_files(tmp_path, rules_dir, fake_hash, monkeypatch):
    import polars as pl

    sample, rec = _sample(tmp_path, rows=5)
    staging = sample / "02_processed" / "staging" / SRC
    (staging / "book1.jsonl").unlink()
    for n in (1, 2):
        pl.DataFrame(
            [{"id": f"id{n}", "text": "t", "source_file": f"book1.tar.gz__part0000{n}"}]
        ).write_parquet(staging / f"p{n}.parquet")
    seen = {}

    def fake_load(conn, source, source_files, *, staging_rows=None):
        seen["source_files"] = source_files
        return {"rows_loaded": 2, "orphan_bodies": 0, "rows_loaded_equals_staging": True}

    monkeypatch.setattr(gate_report, "measure_load", fake_load)
    _fake_connect(monkeypatch, object())
    gate.main(_report_args(sample, rec, tmp_path / "prod", "load"))
    assert seen["source_files"] == ["book1.tar.gz__part00001", "book1.tar.gz__part00002"]


# --- final fix: the caps come from the part markers that wrote the parts ------


class _Prof:
    part_rows = 2
    part_bytes = 10**9


def _parts_sample(tmp_path: Path, caps: list[tuple[int | None, int | None]]) -> tuple[Path, Path]:
    """A sample whose staging holds parts of 3 rows, written with ``caps``."""
    from episteme.data.checkpoint_markers import mark_success

    sample, rec = _sample(tmp_path, rows=0)
    staging = sample / "02_processed" / "staging" / SRC
    (staging / "book1.jsonl").unlink()
    for n, (rows, nbytes) in enumerate(caps, start=1):
        psf = f"book1.tar.gz__part{n:05d}"
        lines = [
            json.dumps({"id": f"p{n}-{i}", "source": SRC, "text": "t" * 50, "source_file": psf})
            for i in range(3)
        ]
        (staging / f"{psf}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        mark_success(
            sample / "02_processed", SRC, psf, stats={"part_rows": rows, "part_bytes": nbytes}
        )
    return sample, rec


@pytest.fixture
def parts_rules(rules_dir, monkeypatch):
    from episteme.ops import profile

    (rules_dir / f"{SRC}.json").write_text(
        json.dumps({"extract": {"parts_within_caps": True}}), encoding="utf-8"
    )
    monkeypatch.setattr(profile, "get_profile", lambda: _Prof())
    return rules_dir


def test_report_takes_the_caps_from_the_part_markers(tmp_path, parts_rules, fake_hash):
    # The parts were written with part_rows=3; the profile now says 2.
    sample, rec = _parts_sample(tmp_path, [(3, 10**6), (3, 10**6)])
    assert gate.main(_report_args(sample, rec, tmp_path / "prod")) == 0
    m = _measures(sample)
    assert (m["part_rows"], m["part_bytes"]) == (3, 10**6)


def test_report_fails_when_the_part_markers_disagree_on_the_caps(
    tmp_path, parts_rules, fake_hash, capsys
):
    sample, rec = _parts_sample(tmp_path, [(3, 10**6), (4, 10**6)])
    assert gate.main(_report_args(sample, rec, tmp_path / "prod")) == 1
    assert "disagree" in capsys.readouterr().out


def test_report_fails_when_a_part_marker_has_no_caps(tmp_path, parts_rules, fake_hash, capsys):
    sample, rec = _parts_sample(tmp_path, [(3, 10**6), (None, None)])
    assert gate.main(_report_args(sample, rec, tmp_path / "prod")) == 1
    assert "part_rows" in capsys.readouterr().out


def test_report_without_part_markers_uses_the_profile(tmp_path, parts_rules, fake_hash):
    sample, rec = _parts_sample(tmp_path, [(3, 10**6)])
    for p in (sample / "02_processed" / "_ops" / SRC / "success").glob("*.ok"):
        p.unlink()
    assert gate.main(_report_args(sample, rec, tmp_path / "prod")) == 1  # 3 rows > 2
    assert _measures(sample)["part_rows"] == 2


def test_run_records_a_stage_that_a_stop_ended_as_interrupted(tmp_path):
    # Review (Greptile): on Windows the stage ends by the stop file with rc 143
    # and gate run sees no signal; the record must still say interrupted.
    rec = tmp_path / "rec.json"
    rc = gate.main(
        ["run", "--record", str(rec), "--", sys.executable, "-c", "raise SystemExit(143)"]
    )
    assert rc == 143
    assert json.loads(rec.read_text(encoding="utf-8"))["interrupted"] is True


def test_run_does_not_mark_a_plain_failure_interrupted(tmp_path):
    rec = tmp_path / "rec.json"
    rc = gate.main(["run", "--record", str(rec), "--", sys.executable, "-c", "raise SystemExit(1)"])
    assert rc == 1
    assert "interrupted" not in json.loads(rec.read_text(encoding="utf-8"))


def test_run_sigterm_ends_a_descendant_that_outlives_the_stage(tmp_path, monkeypatch):
    # Review (CodeRabbit): the child exits within the grace wait, but its own
    # child keeps running; gate run must end it.
    import time

    import psutil

    monkeypatch.setattr(gate, "POLL_S", 0.1)
    monkeypatch.setattr(gate, "STOP_GRACE_S", 20.0)
    pidfile = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time, pathlib;"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']);"
        f"pathlib.Path(r'{pidfile}').write_text(str(p.pid));"
        "time.sleep(2.0)"
    )
    _sigterm_once_started(monkeypatch)
    rec = tmp_path / "rec.json"
    rc = gate.main(["run", "--record", str(rec), "--", sys.executable, "-c", code])
    assert rc == 143
    gpid = int(pidfile.read_text())

    def running(pid):
        # A zombie (ended, not reaped by a PID 1 that does not reap) counts as ended.
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    deadline = time.monotonic() + 10
    while running(gpid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not running(gpid)
