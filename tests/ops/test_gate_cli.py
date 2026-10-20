"""The gate command line (ops/gate.py ``main``)."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from episteme.ops import gate

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
    rec.write_text(json.dumps({"elapsed_s": 2.0, "rc": 0, "peak_rss_mb": 123.0}), encoding="utf-8")
    return sample, rec


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
    rec.write_text(json.dumps({"elapsed_s": 1.0, "rc": 3, "peak_rss_mb": 1.0}), encoding="utf-8")
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


def test_migrations_check(tmp_path, monkeypatch, capsys):
    mig = tmp_path / "mig"
    mig.mkdir()
    for n in ("0001_a.sql", "0002_b.sql", "0003_c.sql"):
        (mig / n).write_text("select 1;")
    monkeypatch.setattr(gate, "_MIGRATIONS_DIR", mig)

    _fake_connect(monkeypatch, _Conn(migrations=["0001_a.sql"]))
    assert gate.main(["migrations-check"]) == 1
    assert capsys.readouterr().out.split() == ["0002_b.sql", "0003_c.sql"]

    _fake_connect(monkeypatch, _Conn(migrations=["0001_a.sql", "0002_b.sql", "0003_c.sql", "x"]))
    assert gate.main(["migrations-check"]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.pg
def test_migrations_check_against_episteme_test():
    from episteme.data.db.connection import connection

    with connection() as conn, conn.cursor() as cur:
        cur.execute("select current_database(), current_setting('server_version')")
        assert cur.fetchone() == ("episteme_test", "19beta3")
    assert gate.main(["migrations-check"]) in (0, 1)


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


def test_migrations_check_without_tracking_table_lists_all(tmp_path, monkeypatch, capsys):
    from psycopg import errors

    mig = tmp_path / "mig"
    mig.mkdir()
    (mig / "0001_a.sql").write_text("select 1;")
    monkeypatch.setattr(gate, "_MIGRATIONS_DIR", mig)

    class Cur(_Cur):
        def execute(self, sql, params=None):
            raise errors.UndefinedTable("no table")

    class Conn(_Conn):
        rolled_back = False

        def cursor(self):
            return Cur({})

        def rollback(self):
            self.rolled_back = True

    conn = Conn()
    _fake_connect(monkeypatch, conn)
    assert gate.main(["migrations-check"]) == 1
    assert capsys.readouterr().out.split() == ["0001_a.sql"]
    assert conn.rolled_back
