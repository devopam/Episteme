"""SP7 Task 2 -- CDISC Controlled Terminology serializer driver.

Fixture: tests/fixtures/sp7/cdisc_ct/SDTM_Terminology.txt (Task 1's fixture,
reused here). Raw trees are built under tmp_path by copying it into
<raw>/<package>/<release_date>/<package>_Terminology.txt, mirroring the real
NCI EVS download layout (design spec sec 4.2).

Staging path confirmed against staging_writer.write_parquet_shard (not
guessed): shards land at <out>/staging/<source>/<stem>.parquet, where stem is
Path(source_file).name with only .gz/.xml suffixes stripped -- source_file
here is input_key(path, raw_dir), e.g.
"SDTM__2026-09-25__SDTM_Terminology.txt", which is NOT stripped (its
extension is .txt), so the produced shard filename is exactly
"SDTM__2026-09-25__SDTM_Terminology.txt.parquet" -- confirms the substring
check below without guessing the writer's behaviour.

All ``not pg`` -- no DB.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import polars as pl

from episteme.data import article_schema
from episteme.data.cdisc_ct.serialize_cdisc_ct import (
    discover_cdisc_ct_files,
    serialize_cdisc_ct,
)

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp7" / "cdisc_ct" / "SDTM_Terminology.txt"


def _place(raw, package, date):
    d = raw / package / date
    d.mkdir(parents=True)
    shutil.copy(FX, d / f"{package}_Terminology.txt")
    return d / f"{package}_Terminology.txt"


def test_source_registered():
    assert "cdisc_ct" in article_schema.SOURCES


def test_discovers_newest_release_per_package(tmp_path):
    raw = tmp_path / "raw"
    _place(raw, "SDTM", "2026-06-26")
    newest = _place(raw, "SDTM", "2026-09-25")
    proto = _place(raw, "Protocol", "2026-07-11")  # different date from SDTM
    found = discover_cdisc_ct_files(raw)
    assert found == [newest, proto]


def test_serializes_and_is_restartable(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    _place(raw, "SDTM", "2026-09-25")
    first = serialize_cdisc_ct(raw, out)
    assert first["inputs"] == 1 and first["ok"] == 1 and first["rows"] > 0
    shards = list((out / "staging" / "cdisc_ct").glob("*"))
    assert len(shards) == 1 and "SDTM__2026-09-25__SDTM_Terminology" in shards[0].name
    df = pl.read_parquet(shards[0])
    assert df["id"].str.starts_with("cdisc_ct:SDTM:").all()
    again = serialize_cdisc_ct(raw, out)
    assert again["rows"] == 0  # skipped via marker


def test_new_release_is_processed(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    _place(raw, "SDTM", "2026-06-26")
    serialize_cdisc_ct(raw, out)
    _place(raw, "SDTM", "2026-09-25")
    res = serialize_cdisc_ct(raw, out)
    assert res["ok"] == 1 and res["rows"] > 0


def test_bad_file_is_marked_failed_not_partial(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    d = raw / "SDTM" / "2026-09-25"
    d.mkdir(parents=True)
    (d / "SDTM_Terminology.txt").write_text("<html>fallback</html>\n", encoding="utf-8")
    res = serialize_cdisc_ct(raw, out)
    assert res["failed"] == 1 and res["rows"] == 0
    assert (
        not list((out / "staging" / "cdisc_ct").glob("*"))
        if (out / "staging" / "cdisc_ct").exists()
        else True
    )
