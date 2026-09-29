"""SP7 Task 4 -- episteme.data.cdisc_ct.retire CLI (marker/keep-set logic).

All ``not pg`` -- these paths return before opening a DB connection, so no
network or Postgres is needed. The DB-touching success path is exercised by
tests/data/test_retire_source_files.py (retire_source_files itself, pg-marked)
plus a monkeypatched call in this file to pin the exact keep_source_files and
shard-name-with-extension marker check without a live connection.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from episteme.data.cdisc_ct import retire

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp7" / "cdisc_ct" / "SDTM_Terminology.txt"


def _place(raw: Path, package: str, date: str) -> Path:
    d = raw / package / date
    d.mkdir(parents=True)
    shutil.copy(FX, d / f"{package}_Terminology.txt")
    return d / f"{package}_Terminology.txt"


def test_no_files_discovered_is_a_noop(tmp_path, capsys):
    raw = tmp_path / "01_raw" / "cdisc_ct"
    raw.mkdir(parents=True)
    processed = tmp_path / "02_processed"

    rc = retire.main(["--raw-dir", str(raw), "--processed-dir", str(processed)])

    assert rc == 0
    assert "nothing to retire" in capsys.readouterr().out


def test_missing_load_success_marker_skips_without_deleting(tmp_path, capsys, monkeypatch):
    raw = tmp_path / "01_raw" / "cdisc_ct"
    _place(raw, "SDTM", "2026-09-25")
    processed = tmp_path / "02_processed"  # no _ops tree at all -> no marker

    def _fail_if_called(*a, **k):
        raise AssertionError("retire_source_files must not be called when a marker is missing")

    monkeypatch.setattr(
        "episteme.data.cdisc_ct.retire.postgres_loader.retire_source_files", _fail_if_called
    )

    rc = retire.main(["--raw-dir", str(raw), "--processed-dir", str(processed)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "not loaded yet" in out
    assert "SDTM__2026-09-25__SDTM_Terminology.txt" in out


def test_keep_set_uses_shard_filename_with_extension_for_the_marker(tmp_path, monkeypatch):
    """The load_success marker is keyed on the SHARD file name (input_key +
    '.parquet' or '.jsonl'), not the bare input_key -- confirmed against
    load_articles.py / staging_writer.py (see retire.py's module docstring).
    A marker written at the bare input_key must NOT be treated as loaded."""
    raw = tmp_path / "01_raw" / "cdisc_ct"
    _place(raw, "SDTM", "2026-09-25")
    processed = tmp_path / "02_processed"

    basename = "SDTM__2026-09-25__SDTM_Terminology.txt"

    # wrong marker name (bare input_key, no shard extension) -> still "missing"
    wrong = processed / "_ops" / "cdisc_ct" / "load_success" / f"{basename}.ok"
    wrong.parent.mkdir(parents=True)
    wrong.write_text("{}", encoding="utf-8")

    calls = []
    monkeypatch.setattr(
        "episteme.data.cdisc_ct.retire.postgres_loader.retire_source_files",
        lambda conn, **kw: calls.append(kw) or 0,
    )
    monkeypatch.setattr(
        "episteme.data.cdisc_ct.retire.connection",
        lambda: _FakeConnCtx(),
    )
    # This test is about the marker-filename-extension logic, not I2's DB
    # verification (which needs a real cursor) -- stub it out as confirmed.
    monkeypatch.setattr(
        "episteme.data.cdisc_ct.retire._verify_before_retire",
        lambda conn, **kw: (True, ""),
    )

    rc = retire.main(["--raw-dir", str(raw), "--processed-dir", str(processed)])
    assert rc == 0
    assert calls == []  # not loaded yet under the real shard-name marker

    # correct marker name (shard file name, with the .parquet extension
    # staging_writer actually produces) -> now treated as loaded.
    correct = processed / "_ops" / "cdisc_ct" / "load_success" / f"{basename}.parquet.ok"
    correct.write_text("{}", encoding="utf-8")

    rc = retire.main(["--raw-dir", str(raw), "--processed-dir", str(processed)])
    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["source"] == "cdisc_ct"
    assert calls[0]["keep_source_files"] == [basename]


class _FakeConn:
    def commit(self):
        pass


class _FakeConnCtx:
    def __enter__(self):
        return _FakeConn()

    def __exit__(self, *exc):
        return False
