"""Shared parts retire path (pubchem and pmc)."""

from __future__ import annotations

import contextlib
import json

import pytest

from episteme.data import parts_retire
from episteme.data.checkpoint_markers import load_success_marker_path, success_marker_path
from episteme.data.pmc import retire as pmc_retire


def test_pending_files_are_per_source(tmp_path):
    parts_retire.add_retire_pending(tmp_path, "pmc", ["a__part00001"])
    parts_retire.add_retire_pending(tmp_path, "pubchem", ["b__part00001"])
    assert parts_retire.read_retire_pending(tmp_path, "pmc") == ["a__part00001"]
    assert parts_retire.read_retire_pending(tmp_path, "pubchem") == ["b__part00001"]
    assert parts_retire.retire_pending_path(tmp_path, "pmc") == (
        tmp_path / "_ops" / "pmc" / "retire_pending.json"
    )
    assert parts_retire.retire_pending_path(tmp_path, "pubchem") == (
        tmp_path / "_ops" / "pubchem" / "retire_pending.json"
    )


def _touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}", encoding="utf-8")


def test_classify_pending_load_marker_first_for_pmc(tmp_path):
    loaded, waiting, gone = "l__part00001", "w__part00001", "g__part00001"
    _touch(load_success_marker_path(tmp_path, "pmc", f"{loaded}.parquet"))
    _touch(success_marker_path(tmp_path, "pmc", loaded))  # both markers: load wins
    _touch(success_marker_path(tmp_path, "pmc", waiting))
    assert parts_retire.classify_pending(tmp_path, "pmc", [gone, loaded, waiting]) == (
        [gone],
        [loaded],
        [waiting],
    )


def test_pmc_retire_with_no_pending_file_touches_no_database(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not connect")

    monkeypatch.setattr("episteme.data.db.connection.connection", _boom)
    monkeypatch.setattr("episteme.ops.lock.advisory_lock", _boom)
    assert pmc_retire.main(["--processed-dir", str(tmp_path)]) == 0


def test_bad_pending_file_exits_one(tmp_path):
    p = parts_retire.retire_pending_path(tmp_path, "pmc")
    p.parent.mkdir(parents=True)
    p.write_text('{"x": 1}', encoding="utf-8")
    assert pmc_retire.main(["--processed-dir", str(tmp_path)]) == 1


def test_main_requires_source():
    with pytest.raises(TypeError):
        parts_retire.main([])  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# per-unit pending files (retire_pending.d/<unit>.json): one writer per file
# --------------------------------------------------------------------------- #


def _unit_file(out, unit):
    return out / "_ops" / "pmc" / "retire_pending.d" / f"{unit}.json"


def test_unit_pending_files_merge_and_read_as_a_union(tmp_path):
    parts_retire.add_retire_pending(tmp_path, "pmc", ["m__part00001"])
    parts_retire.add_retire_pending(tmp_path, "pmc", ["b0__part00002"], unit="b0")
    parts_retire.add_retire_pending(tmp_path, "pmc", ["b0__part00001"], unit="b0")
    parts_retire.add_retire_pending(tmp_path, "pmc", ["b1__part00001", "m__part00001"], unit="b1")
    assert json.loads(_unit_file(tmp_path, "b0").read_text("utf-8")) == [
        "b0__part00001",
        "b0__part00002",
    ]
    # the main file holds only what was added without a unit
    assert json.loads(parts_retire.retire_pending_path(tmp_path, "pmc").read_text("utf-8")) == [
        "m__part00001"
    ]
    assert parts_retire.read_retire_pending(tmp_path, "pmc") == [
        "b0__part00001",
        "b0__part00002",
        "b1__part00001",
        "m__part00001",
    ]


def test_a_bad_unit_pending_file_raises(tmp_path):
    p = _unit_file(tmp_path, "b0")
    p.parent.mkdir(parents=True)
    p.write_text('{"x": 1}', encoding="utf-8")
    with pytest.raises(ValueError):
        parts_retire.read_retire_pending(tmp_path, "pmc")
    assert pmc_retire.main(["--processed-dir", str(tmp_path)]) == 1


@pytest.fixture
def fake_db(monkeypatch):
    calls: list[list[str]] = []

    class _G:
        def exit_code(self, rc):
            return rc

    class _Conn:
        def commit(self):
            pass

    @contextlib.contextmanager
    def _guard(conn_ref, **kw):
        yield _G()

    @contextlib.contextmanager
    def _advisory(**kw):
        yield

    @contextlib.contextmanager
    def _connection():
        yield _Conn()

    def _delete(conn, *, source, source_files, run_id):
        calls.append(list(source_files))
        return len(source_files)

    monkeypatch.setattr("episteme.ops.stop.stage_guard", _guard)
    monkeypatch.setattr("episteme.ops.lock.advisory_lock", _advisory)
    monkeypatch.setattr("episteme.data.db.connection.connection", _connection)
    monkeypatch.setattr(parts_retire.postgres_loader, "delete_source_files", _delete)
    return calls


def test_retire_merges_unit_files_and_removes_only_those_it_read(tmp_path, monkeypatch, fake_db):
    out = tmp_path / "out"
    gone, loaded, waiting = "pmc_b0000__part00002", "pmc_b0001__part00002", "pmc_b0001__part00003"
    main_gone = "pmc_b0005__part00001"
    late = "pmc_b0009__part00001"
    parts_retire.add_retire_pending(out, "pmc", [main_gone])
    parts_retire.add_retire_pending(out, "pmc", [gone], unit="pmc_b0000")
    parts_retire.add_retire_pending(out, "pmc", [loaded, waiting], unit="pmc_b0001")
    _touch(load_success_marker_path(out, "pmc", f"{loaded}.parquet"))
    _touch(success_marker_path(out, "pmc", waiting))

    real_classify = parts_retire.classify_pending

    def _classify_then_late_writer(processed_dir, source, pending):
        # A bucket rebuild writes its unit file after the retire step read.
        parts_retire.add_retire_pending(out, "pmc", [late], unit="pmc_b0009")
        return real_classify(processed_dir, source, pending)

    monkeypatch.setattr(parts_retire, "classify_pending", _classify_then_late_writer)
    assert pmc_retire.main(["--processed-dir", str(out)]) == 0

    assert fake_db == [[gone, main_gone]]
    assert not _unit_file(out, "pmc_b0000").exists()
    assert not _unit_file(out, "pmc_b0001").exists()
    assert json.loads(_unit_file(out, "pmc_b0009").read_text("utf-8")) == [late]
    assert json.loads(parts_retire.retire_pending_path(out, "pmc").read_text("utf-8")) == [waiting]
    assert parts_retire.read_retire_pending(out, "pmc") == [waiting, late]


def test_a_unit_file_changed_after_the_read_is_kept(tmp_path, monkeypatch, fake_db):
    out = tmp_path / "out"
    a, b = "pmc_b0000__part00002", "pmc_b0000__part00003"
    parts_retire.add_retire_pending(out, "pmc", [a], unit="pmc_b0000")
    real_classify = parts_retire.classify_pending

    def _classify_then_merge(processed_dir, source, pending):
        parts_retire.add_retire_pending(out, "pmc", [b], unit="pmc_b0000")
        return real_classify(processed_dir, source, pending)

    monkeypatch.setattr(parts_retire, "classify_pending", _classify_then_merge)
    assert pmc_retire.main(["--processed-dir", str(out)]) == 0
    assert fake_db == [[a]]
    # the changed file stays; the name already deleted goes again next time (0 rows)
    assert parts_retire.read_retire_pending(out, "pmc") == [a, b]
