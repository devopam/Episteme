"""Extractors (and the openalex serializer) stop at the first failed file,
retry transient errors, record the failed unit, and take ``--skip-unit``."""

from __future__ import annotations

import errno
import importlib
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from episteme.config import get_settings
from episteme.data import parallel
from episteme.data.checkpoint_markers import input_key, is_success
from episteme.ops import skip
from episteme.ops.retry import TransientUnitError

REPO = Path(__file__).resolve().parents[2]
SP2 = REPO / "tests" / "fixtures" / "sp2"
CASES = [
    ("episteme.data.pubmed.extract_pubmed", "extract_pubmed", SP2 / "pubmed", "extract"),
    ("episteme.data.pmc.extract_pmc", "extract_pmc", REPO / "tests/data/fixtures/pmc", "extract"),
    (
        "episteme.data.bookshelf.extract_bookshelf",
        "extract_bookshelf",
        SP2 / "bookshelf",
        "extract",
    ),
    (
        "episteme.data.guidelines.extract_guidelines",
        "extract_guidelines",
        SP2 / "guidelines",
        "extract",
    ),
    ("episteme.data.apollo.extract_apollo", "extract_apollo", SP2 / "apollo", "extract"),
    (
        "episteme.data.europepmc.preprints.extract_europepmc_preprints",
        "extract_europepmc_preprints",
        SP2 / "europepmc_preprint",
        "extract",
    ),
    (
        "episteme.data.europepmc.manuscripts.extract_europepmc_manuscripts",
        "extract_europepmc_manuscripts",
        SP2 / "europepmc_manuscript",
        "extract",
    ),
    (
        "episteme.data.openalex.serialize_openalex",
        "serialize_openalex",
        REPO / "tests/fixtures/sp4/openalex",
        "serialize",
    ),
]
IDS = [c[0].rsplit(".", 1)[1] for c in CASES]


@pytest.fixture
def root(monkeypatch, tmp_path):
    """processed_root (and so last_failure.json) under tmp_path."""
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("EPISTEME_RUN_ID", "failfast-1")
    get_settings.cache_clear()
    yield tmp_path / "root"
    get_settings.cache_clear()


@pytest.fixture
def slept(monkeypatch):
    """Replace run_per_file's default sleep: no real 30/60/120 s waits."""
    calls: list[float] = []
    monkeypatch.setattr(parallel, "_SLEEP", calls.append)
    return calls


def _last_failure(root: Path, mod) -> dict:
    return json.loads((root / "_ops" / mod.SOURCE / "last_failure.json").read_text("utf-8"))


@pytest.mark.parametrize("module,func,raw_dir,stage", CASES, ids=IDS)
def test_transient_error_is_retried_then_recorded(
    tmp_path, root, slept, monkeypatch, module, func, raw_dir, stage
):
    mod = importlib.import_module(module)

    def _eio(*_a, **_k):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(mod, "write_rows", _eio)
    res = getattr(mod, func)(raw_dir, tmp_path / "out", workers=1)
    assert res["failed"] == 1
    assert slept == [30, 60, 120]
    rec = _last_failure(root, mod)
    assert rec["stage"] == stage and rec["run_id"] == "failfast-1"
    assert "TransientUnitError" in rec["error"] and "I/O error" in rec["error"]
    keys = {input_key(f, raw_dir) for f in raw_dir.rglob("*") if f.is_file()}
    assert rec["unit"] in keys  # the input_key --skip-unit takes


@pytest.mark.parametrize("module,func,raw_dir,stage", CASES, ids=IDS)
def test_permanent_error_is_not_retried_and_main_returns_1(
    tmp_path, root, slept, monkeypatch, capsys, module, func, raw_dir, stage
):
    mod = importlib.import_module(module)

    def _bad(*_a, **_k):
        raise ValueError("bad row")

    monkeypatch.setattr(mod, "write_rows", _bad)
    argv = ["--raw-dir", str(raw_dir), "--processed-dir", str(tmp_path / "out"), "--workers", "1"]
    assert mod.main(argv) == 1
    assert slept == []
    rec = _last_failure(root, mod)
    assert rec["stage"] == stage
    assert "bad row" in rec["error"]
    assert f"stopped at {rec['unit']}" in capsys.readouterr().err


@pytest.mark.parametrize("module,func,raw_dir,stage", CASES, ids=IDS)
def test_process_one_raises_transient_unit_error(
    tmp_path, monkeypatch, module, func, raw_dir, stage
):
    """Classification happens in the worker, where the real exception exists."""
    mod = importlib.import_module(module)

    def _timeout(*_a, **_k):
        raise TimeoutError("disk timed out")

    monkeypatch.setattr(mod, "write_rows", _timeout)
    files = sorted(p for p in raw_dir.rglob("*") if p.is_file())
    raised = 0
    for path in files:
        try:
            mod.process_one(path, raw_dir=raw_dir, processed_dir=tmp_path / "out", force=True)
        except TransientUnitError as exc:
            assert "disk timed out" in exc.cause
            raised += 1
    assert raised >= 1


# --- pmc: fail fast across files, --skip-unit --------------------------------

from episteme.data.pmc import extract_pmc as pmc  # noqa: E402

_FIX = REPO / "tests" / "data" / "fixtures" / "pmc"


def _pmc_raw(tmp_path: Path, n: int = 3) -> Path:
    """n copies of the PMC fixture as PMCFIX000{i}.1.json (+ .xml)."""
    raw = tmp_path / "raw"
    raw.mkdir()
    meta = (_FIX / "PMCFIX0001.1.json").read_text("utf-8")
    xml = (_FIX / "PMCFIX0001.1.xml").read_text("utf-8")
    for i in range(1, n + 1):
        (raw / f"PMCFIX000{i}.1.json").write_text(meta, encoding="utf-8")
        (raw / f"PMCFIX000{i}.1.xml").write_text(xml, encoding="utf-8")
    return raw


def test_one_worker_stops_at_the_failing_second_file(tmp_path, root, monkeypatch):
    raw = _pmc_raw(tmp_path)
    real = pmc.row_from_meta_and_xml
    called: list[str] = []

    def _row(meta_path, raw_dir):
        called.append(meta_path.name)
        if meta_path.name == "PMCFIX0002.1.json":
            raise ValueError("broken record")
        return real(meta_path, raw_dir)

    monkeypatch.setattr(pmc, "row_from_meta_and_xml", _row)
    out = tmp_path / "out"
    res = pmc.extract_pmc(raw, out, workers=1)
    assert called == ["PMCFIX0001.1.json", "PMCFIX0002.1.json"]  # third never called
    assert (res["inputs"], res["ok"], res["failed"]) == (3, 1, 1)
    assert is_success(out, "pmc", "PMCFIX0001.1.json")
    assert not is_success(out, "pmc", "PMCFIX0003.1.json")
    assert _last_failure(root, pmc)["unit"] == "PMCFIX0002.1.json"


@pytest.fixture
def fake_skip_db(monkeypatch):
    rows: list[tuple] = []

    class _Conn:
        def commit(self):
            pass

        def rollback(self):
            pass

    @contextmanager
    def _connection():
        yield _Conn()

    def _record(event, **kw):
        rows.append((event, kw["object"], kw["reason"]))
        return "h"

    monkeypatch.setattr(skip, "connection", _connection)
    monkeypatch.setattr(skip.audit_trail, "record", _record)
    return rows


def _argv(raw: Path, out: Path, *extra: str) -> list[str]:
    return ["--raw-dir", str(raw), "--processed-dir", str(out), "--workers", "1", *extra]


def test_skip_unit_skips_the_named_file_with_an_audit_row(tmp_path, root, fake_skip_db):
    raw = _pmc_raw(tmp_path)
    out = tmp_path / "out"
    argv = _argv(raw, out, "--skip-unit", "PMCFIX0002.1.json", "--reason", "corrupt upstream")
    assert pmc.main(argv) == 0
    assert fake_skip_db == [("unit_skip", "pmc PMCFIX0002.1.json", "corrupt upstream")]
    assert is_success(out, "pmc", "PMCFIX0001.1.json")
    assert not is_success(out, "pmc", "PMCFIX0002.1.json")
    assert is_success(out, "pmc", "PMCFIX0003.1.json")


def test_unknown_skip_unit_exits_2_before_any_file(tmp_path, root, fake_skip_db):
    raw = _pmc_raw(tmp_path)
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as e:
        pmc.main(_argv(raw, out, "--skip-unit", "nope.json", "--reason", "r"))
    assert e.value.code == 2
    assert fake_skip_db == []
    assert not is_success(out, "pmc", "PMCFIX0001.1.json")


def test_skip_unit_requires_reason(tmp_path, root):
    raw = _pmc_raw(tmp_path, 1)
    with pytest.raises(SystemExit) as e:
        pmc.main(_argv(raw, tmp_path / "out", "--skip-unit", "PMCFIX0001.1.json"))
    assert e.value.code == 2


def test_run_without_skip_unit_opens_no_skip_connection(tmp_path, root, monkeypatch):
    def _no_db(*_a, **_k):
        raise AssertionError("apply_skips opened a connection with nothing to skip")

    monkeypatch.setattr(skip, "connection", _no_db)
    raw = _pmc_raw(tmp_path, 2)
    assert pmc.main(_argv(raw, tmp_path / "out")) == 0


def test_skipping_every_file_is_a_clean_run(tmp_path, root, fake_skip_db, capsys):
    raw = _pmc_raw(tmp_path, 1)
    argv = _argv(raw, tmp_path / "out", "--skip-unit", "PMCFIX0001.1.json", "--reason", "r")
    assert pmc.main(argv) == 0
    out = capsys.readouterr()
    assert "done inputs=0 ok=0 failed=0" in out.out
    assert "ERROR" not in out.err
    assert len(fake_skip_db) == 1


def test_empty_raw_dir_still_fails(tmp_path, root):
    raw = tmp_path / "raw"
    raw.mkdir()
    assert pmc.main(_argv(raw, tmp_path / "out")) == 1


@pytest.mark.parametrize("module", [c[0] for c in CASES if "extract_pmc" not in c[0]])
def test_skip_unit_is_rejected_with_report(tmp_path, root, module):
    mod = importlib.import_module(module)
    raw = tmp_path / "raw"
    raw.mkdir()
    argv = _argv(raw, tmp_path / "out", "--report", "--skip-unit", "x", "--reason", "r")
    with pytest.raises(SystemExit) as e:
        mod.main(argv)
    assert e.value.code == 2


# --- openalex: the same basename in every partition (R7) ----------------------

from episteme.data.openalex import serialize_openalex as oa  # noqa: E402

_OA_PARTS = ("updated_date=2024-01-01", "updated_date=2024-02-01")
_OA_KEYS = [f"{d}__part_0000.gz" for d in _OA_PARTS]


def _oa_raw(tmp_path: Path) -> Path:
    import gzip

    raw = tmp_path / "oa_raw"
    data = (REPO / "tests/fixtures/sp4/openalex/sample.jsonl").read_bytes()
    for d in _OA_PARTS:
        (raw / d).mkdir(parents=True)
        (raw / d / "part_0000.gz").write_bytes(gzip.compress(data))
    return raw


def test_bare_basename_matching_two_partitions_exits_2_and_lists_both(
    tmp_path, root, fake_skip_db, capsys
):
    raw = _oa_raw(tmp_path)
    with pytest.raises(SystemExit) as e:
        oa.main(_argv(raw, tmp_path / "out", "--skip-unit", "part_0000.gz", "--reason", "r"))
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert all(k in err for k in _OA_KEYS)
    assert fake_skip_db == []
    assert not is_success(tmp_path / "out", "openalex", _OA_KEYS[0])


def test_key_skip_skips_exactly_one_partition(tmp_path, root, fake_skip_db):
    raw = _oa_raw(tmp_path)
    out = tmp_path / "out"
    assert oa.main(_argv(raw, out, "--skip-unit", _OA_KEYS[0], "--reason", "r")) == 0
    assert fake_skip_db == [("unit_skip", f"openalex {_OA_KEYS[0]}", "r")]
    assert not is_success(out, "openalex", _OA_KEYS[0])
    assert is_success(out, "openalex", _OA_KEYS[1])


def test_failure_record_names_the_partition_key(tmp_path, root, slept, monkeypatch):
    raw = _oa_raw(tmp_path)

    def _bad(*_a, **_k):
        raise ValueError("bad row")

    monkeypatch.setattr(oa, "write_rows", _bad)
    assert oa.main(_argv(raw, tmp_path / "out")) == 1
    assert _last_failure(root, oa)["unit"] == _OA_KEYS[0]
