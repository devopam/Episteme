"""download_pmc: a pause point before each submit batch; failed PMCIDs go to
<run dir>/failed_files.txt."""

from __future__ import annotations

import sys

import pytest

from episteme.data.pmc import download_pmc as dp


def _env(tmp_path, monkeypatch, *, run_id="R1", with_run_dir=True):
    monkeypatch.setattr("episteme.config._find_project_dotenv", lambda: None)
    monkeypatch.setenv("EPISTEME_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path / "proc"))
    monkeypatch.setenv("EPISTEME_RAW_ROOT", str(tmp_path / "raw"))
    monkeypatch.setenv("EPISTEME_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("EPISTEME_ACTOR", "x")
    monkeypatch.delenv("EPISTEME_RUN_ID", raising=False)
    monkeypatch.delenv("EPISTEME_RUN_DIR", raising=False)
    if run_id:
        monkeypatch.setenv("EPISTEME_RUN_ID", run_id)
    if with_run_dir:
        monkeypatch.setenv("EPISTEME_RUN_DIR", str(tmp_path / "run"))
    import episteme.config as cfg

    cfg.get_settings.cache_clear()
    return cfg


def _go(monkeypatch, tmp_path, inv, load):
    out = tmp_path / "oa_comm"
    monkeypatch.setattr(dp, "fetch_inventory", lambda dest: ("d", inv))
    monkeypatch.setattr(dp, "load_metadata", load)
    monkeypatch.setattr(
        dp, "download_to_file", lambda url, p, dry_run=False, retries=3: p.write_text("x") or True
    )
    monkeypatch.setattr(sys, "argv", ["download_pmc", "--output_dir", str(out), "--limit", "0"])


def _ok(vid):
    return {"license": "CC BY", "pmcid": vid}


def test_pause_point_runs_once_per_submit_batch(tmp_path, monkeypatch):
    cfg = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "SUBMIT_BATCH", 2)
    calls = []
    monkeypatch.setattr(dp.ops_lock, "pause_point", lambda run_id, **kw: calls.append(run_id))
    _go(monkeypatch, tmp_path, {n: 1 for n in range(1, 6)}, lambda vid: None)
    try:
        dp.main()
    finally:
        cfg.get_settings.cache_clear()
    assert calls == ["R1", "R1", "R1"]  # 5 ids in batches of 2


def test_no_run_id_means_no_pause_point(tmp_path, monkeypatch):
    cfg = _env(tmp_path, monkeypatch, run_id=None)
    calls = []
    monkeypatch.setattr(dp.ops_lock, "pause_point", lambda run_id, **kw: calls.append(run_id))
    _go(monkeypatch, tmp_path, {1: 1}, lambda vid: None)
    try:
        dp.main()
    finally:
        cfg.get_settings.cache_clear()
    assert calls == []


def test_failed_pmcid_lands_in_failed_files(tmp_path, monkeypatch):
    cfg = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(dp.ops_lock, "pause_point", lambda run_id, **kw: False)

    def boom(vid):
        raise dp.FetchError("GET x: timed out")

    _go(monkeypatch, tmp_path, {9: 1}, boom)
    try:
        with pytest.raises(SystemExit) as exc:
            dp.main()
    finally:
        cfg.get_settings.cache_clear()
    assert exc.value.code == 1
    lines = (tmp_path / "run" / "failed_files.txt").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    pmcid, reason = lines[0].split("\t")
    assert pmcid == "PMC9" and "request failed" in reason


def test_failed_reason_with_tab_and_newline_stays_one_record(tmp_path, monkeypatch):
    cfg = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(dp.ops_lock, "pause_point", lambda run_id, **kw: False)

    def boom(vid):
        raise dp.FetchError("GET x:\tbad\r\nline two")

    _go(monkeypatch, tmp_path, {9: 1}, boom)
    try:
        with pytest.raises(SystemExit):
            dp.main()
    finally:
        cfg.get_settings.cache_clear()
    text = (tmp_path / "run" / "failed_files.txt").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) == 1 and text.endswith("\n")
    pmcid, reason = lines[0].split("\t")
    assert pmcid == "PMC9" and "bad" in reason and "line two" in reason


def test_progress_once_per_submit_batch(tmp_path, monkeypatch):
    """I3: under run_pipeline.sh (a run id) each submit batch writes progress."""
    from episteme.ops import status

    cfg = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "SUBMIT_BATCH", 2)
    monkeypatch.setattr(dp.ops_lock, "pause_point", lambda run_id, **kw: False)
    seen = []
    real = status.RunStatus.progress

    def spy(self, **kw):
        seen.append((kw["units_done"], kw["units_total"]))
        return real(self, **kw)

    monkeypatch.setattr(status.RunStatus, "progress", spy)
    _go(monkeypatch, tmp_path, {n: 1 for n in range(1, 6)}, lambda vid: None)
    try:
        dp.main()
        rec = status.RunStatus(status.run_dir("R1")).read()
    finally:
        cfg.get_settings.cache_clear()
    assert seen == [(2, 5), (4, 5), (5, 5)]
    assert rec["units_done"] == 5 and rec["units_total"] == 5 and rec["last_progress"]


def test_no_run_id_writes_no_progress(tmp_path, monkeypatch):
    cfg = _env(tmp_path, monkeypatch, run_id=None)
    monkeypatch.setattr(dp.ops_lock, "pause_point", lambda run_id, **kw: False)
    _go(monkeypatch, tmp_path, {1: 1}, lambda vid: None)
    try:
        dp.main()
    finally:
        cfg.get_settings.cache_clear()
    assert not (tmp_path / "proc" / "_ops").exists()
