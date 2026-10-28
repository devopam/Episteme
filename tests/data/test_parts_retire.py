"""Shared parts retire path (pubchem and pmc)."""

from __future__ import annotations

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
