"""The eight thread-model serializers stop at the first failed file, retry
transient errors, record the failed unit, and take ``--skip-unit``."""

from __future__ import annotations

import errno
import importlib
import json
import shutil
from contextlib import contextmanager
from pathlib import Path

import pytest

from episteme.config import get_settings
from episteme.data import parallel
from episteme.data.checkpoint_markers import input_key, is_success
from episteme.ops import skip
from episteme.ops.retry import TransientUnitError

TESTS = Path(__file__).resolve().parents[1]
SP4 = TESTS / "fixtures" / "sp4"
CDISC_FX = TESTS / "fixtures" / "sp7" / "cdisc_ct" / "SDTM_Terminology.txt"

# (module, importable function, discover function, fixture dir or None for cdisc_ct)
CASES = [
    ("cdisc_ct", "serialize_cdisc_ct", "discover_cdisc_ct_files", None),
    ("chembl", "serialize_chembl", "discover_chembl_files", SP4 / "chembl"),
    ("clinvar", "serialize_clinvar", "discover_clinvar_files", SP4 / "clinvar"),
    ("mesh", "serialize_mesh", "discover_mesh_files", SP4 / "mesh"),
    ("ontologies", "serialize_ontologies", "discover_ontology_files", SP4 / "ontologies"),
    ("pubchem", "serialize_pubchem", "discover_pubchem_files", SP4 / "pubchem"),
    ("reactome", "serialize_reactome", "discover_reactome_files", SP4 / "reactome"),
    ("uniprot", "serialize_uniprot", "discover_uniprot_files", SP4 / "uniprot"),
]
IDS = [c[0] for c in CASES]


def _load(name: str):
    return importlib.import_module(f"episteme.data.{name}.serialize_{name}")


def _raw(tmp_path: Path, fixture: Path | None) -> Path:
    raw = tmp_path / "raw"
    if fixture is None:  # cdisc_ct: <package>/<release_date>/<package>_Terminology.txt
        d = raw / "SDTM" / "2026-09-25"
        d.mkdir(parents=True)
        shutil.copy(CDISC_FX, d / "SDTM_Terminology.txt")
    else:
        shutil.copytree(fixture, raw)
    return raw


@pytest.fixture
def root(monkeypatch, tmp_path):
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("EPISTEME_RUN_ID", "failfast-1")
    get_settings.cache_clear()
    yield tmp_path / "root"
    get_settings.cache_clear()


@pytest.fixture
def slept(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(parallel, "_SLEEP", calls.append)
    return calls


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


def _last_failure(root: Path, mod) -> dict:
    return json.loads((root / "_ops" / mod.SOURCE / "last_failure.json").read_text("utf-8"))


def _argv(raw: Path, out: Path, *extra: str) -> list[str]:
    return ["--raw-dir", str(raw), "--processed-dir", str(out), "--workers", "1", *extra]


@pytest.mark.parametrize("name,func,disc,fixture", CASES, ids=IDS)
def test_transient_error_is_retried_then_recorded(
    tmp_path, root, slept, monkeypatch, name, func, disc, fixture
):
    mod = _load(name)
    raw = _raw(tmp_path, fixture)

    def _eio(*_a, **_k):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(mod, "write_rows", _eio)
    res = getattr(mod, func)(raw, tmp_path / "out", workers=1)
    assert res["failed"] == 1
    assert slept == [30, 60, 120]
    rec = _last_failure(root, mod)
    assert rec["stage"] == "serialize" and rec["run_id"] == "failfast-1"
    assert "TransientUnitError" in rec["error"] and "I/O error" in rec["error"]
    assert rec["unit"] in {input_key(f, raw) for f in getattr(mod, disc)(raw)}


@pytest.mark.parametrize("name,func,disc,fixture", CASES, ids=IDS)
def test_permanent_error_is_not_retried_and_main_returns_1(
    tmp_path, root, slept, monkeypatch, capsys, name, func, disc, fixture
):
    mod = _load(name)
    raw = _raw(tmp_path, fixture)

    def _bad(*_a, **_k):
        raise ValueError("bad row")

    monkeypatch.setattr(mod, "write_rows", _bad)
    assert mod.main(_argv(raw, tmp_path / "out")) == 1
    assert slept == []
    rec = _last_failure(root, mod)
    assert rec["stage"] == "serialize" and "bad row" in rec["error"]
    assert f"stopped at {rec['unit']}" in capsys.readouterr().err


@pytest.mark.parametrize("name,func,disc,fixture", CASES, ids=IDS)
def test_process_one_raises_transient_unit_error(tmp_path, monkeypatch, name, func, disc, fixture):
    mod = _load(name)
    raw = _raw(tmp_path, fixture)

    def _timeout(*_a, **_k):
        raise TimeoutError("disk timed out")

    monkeypatch.setattr(mod, "write_rows", _timeout)
    raised = 0
    for path in getattr(mod, disc)(raw):
        try:
            mod.process_one(path, raw_dir=raw, processed_dir=tmp_path / "out", force=True)
        except TransientUnitError as exc:
            assert "disk timed out" in exc.cause
            raised += 1
    assert raised >= 1


@pytest.mark.parametrize("name,func,disc,fixture", CASES, ids=IDS)
def test_skip_unit_skips_every_named_file_and_all_skipped_is_clean(
    tmp_path, root, fake_skip_db, capsys, name, func, disc, fixture
):
    mod = _load(name)
    raw = _raw(tmp_path, fixture)
    files = getattr(mod, disc)(raw)
    out = tmp_path / "out"
    extra: list[str] = []
    for f in files:
        extra += ["--skip-unit", input_key(f, raw)]
    assert mod.main(_argv(raw, out, *extra, "--reason", "corrupt upstream")) == 0
    assert len(fake_skip_db) == len(files)
    assert all(r[0] == "unit_skip" and r[2] == "corrupt upstream" for r in fake_skip_db)
    assert not any(is_success(out, mod.SOURCE, input_key(f, raw)) for f in files)
    cap = capsys.readouterr()
    assert "done inputs=0 ok=0 failed=0" in cap.out and "ERROR" not in cap.err


@pytest.mark.parametrize("name,func,disc,fixture", CASES, ids=IDS)
def test_skip_unit_rejected_with_report_and_without_reason(
    tmp_path, root, fake_skip_db, name, func, disc, fixture
):
    mod = _load(name)
    raw = _raw(tmp_path, fixture)
    for extra in (["--report", "--skip-unit", "x", "--reason", "r"], ["--skip-unit", "x"]):
        with pytest.raises(SystemExit) as e:
            mod.main(_argv(raw, tmp_path / "out", *extra))
        assert e.value.code == 2
    assert fake_skip_db == []


@pytest.mark.parametrize("name,func,disc,fixture", CASES, ids=IDS)
def test_run_without_skip_unit_opens_no_skip_connection(
    tmp_path, root, monkeypatch, name, func, disc, fixture
):
    def _no_db(*_a, **_k):
        raise AssertionError("apply_skips opened a connection with nothing to skip")

    monkeypatch.setattr(skip, "connection", _no_db)
    mod = _load(name)
    raw = _raw(tmp_path, fixture)
    assert mod.main(_argv(raw, tmp_path / "out")) == 0


def test_serializer_stops_at_the_first_failed_file(tmp_path, root, monkeypatch):
    """ontologies: three .obo files, the second fails, the third never runs."""
    mod = _load("ontologies")
    raw = tmp_path / "raw"
    raw.mkdir()
    for n in ("a", "b", "c"):
        shutil.copy(SP4 / "ontologies" / "sample.obo", raw / f"{n}.obo")
    real = mod.write_rows
    called: list[int] = []

    def _write(*a, **k):
        called.append(1)
        if len(called) == 2:
            raise ValueError("broken record")
        return real(*a, **k)

    monkeypatch.setattr(mod, "write_rows", _write)
    out = tmp_path / "out"
    res = mod.serialize_ontologies(raw, out, workers=1)
    assert len(called) == 2  # the third file never started
    assert (res["inputs"], res["ok"], res["failed"]) == (3, 1, 1)
    assert is_success(out, "ontologies", "a.obo")
    assert not is_success(out, "ontologies", "c.obo")
    assert _last_failure(root, mod)["unit"] == "b.obo"
