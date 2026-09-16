"""SP4 Task 5 -- chembl structured serializer.

Fixture: tests/fixtures/sp4/chembl/sample.db -- a hand-built SQLite file with
a subset of the real ChEMBL 37 release schema (activities /
molecule_dictionary / compound_structures / assays / target_dictionary;
column names verified against ChEMBL's own published
schema_documentation.txt, not guessed). 3 well-populated activity rows
across 2 compounds (CHEMBL25 appears under two different assays/targets),
plus 1 deliberately orphaned activity row (molregno NULL, assay_id with no
matching `assays` row) that exercises the LEFT-JOIN-not-INNER-JOIN fix --
activities.molregno is nullable in the real schema, so an INNER JOIN would
silently drop such records instead of serializing them via the
"unknown compound" / omitted-clause fallback.

All `not pg` -- no DB.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from episteme.data import article_schema
from episteme.data.chembl.serialize_chembl import iter_rows_from_file, main, serialize_chembl

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp4" / "chembl"


def _read_shard(staging_dir: Path) -> pl.DataFrame:
    shards = list(staging_dir.glob("*.parquet")) or list(staging_dir.glob("*.jsonl"))
    assert shards, f"no shard under {staging_dir}"
    s = shards[0]
    return pl.read_parquet(s) if s.suffix == ".parquet" else pl.read_ndjson(s)


def test_chembl_serialize_rows(tmp_path):
    res = serialize_chembl(FX, tmp_path)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["failed"] == 0
    assert res["rows"] == 4

    shards = list((tmp_path / "staging" / "chembl").glob("*.*"))
    assert shards
    df = pl.read_parquet(shards[0]) if shards[0].suffix == ".parquet" else pl.read_ndjson(shards[0])
    rows = df.to_dicts()
    r = rows[0]
    assert r["source"] == "chembl"
    assert r["id"].startswith("chembl:")
    assert r["subset"] == "commercial"  # CC BY-SA 3.0
    assert r["container_id"] is None and r["book_meta"] is None

    assert {row["source"] for row in rows} == {"chembl"}
    assert all(row["id"].startswith("chembl:") for row in rows)
    assert len({row["id"] for row in rows}) == 4  # ids unique per activity row
    assert all(row["subset"] == "commercial" for row in rows)
    assert all(row["license"] == "CC BY-SA" for row in rows)
    assert all(row["container_id"] is None for row in rows)
    assert all(row["book_meta"] is None for row in rows)
    # ChEMBL bioactivity records carry no bibliographic shape.
    assert all(row["title"] is None for row in rows)
    assert all(row["journal"] is None for row in rows)
    assert all(row["year"] is None for row in rows)
    assert all(row["authors"] is None for row in rows)
    assert all(row["pmid"] is None and row["pmcid"] is None and row["doi"] is None for row in rows)
    assert all(row["extract_status"] in article_schema.EXTRACT_STATUSES for row in rows)

    texts = " ".join(row["text"] or "" for row in rows)
    assert "CHEMBL25" in texts
    assert "IC50" in texts
    assert "Cyclooxygenase-2" in texts

    by_id = {row["id"]: row for row in rows}
    # The 3 well-populated rows (compound + target + assay all resolve) must
    # be "ok", not "partial" -- corpus_materializer only selects
    # extract_status='ok' rows into the corpus, so a "partial"-only source
    # would be a silent no-op downstream despite subset="commercial". See
    # serialize_chembl.py's _build_text docstring for why the template's
    # closing clause matters.
    for aid in ("chembl:31863", "chembl:31864", "chembl:31865"):
        assert by_id[aid]["extract_status"] == "ok", by_id[aid]

    # The orphan row (molregno NULL, dangling assay_id) is NOT dropped by the
    # LEFT JOINs -- it is serialized with the "unknown compound" fallback and
    # no target/assay clause, and legitimately lands under MIN_OK_TEXT_LEN so
    # extract_status="partial" (honest low-confidence, not a bug).
    orphan = by_id["chembl:31866"]
    assert "unknown compound" in orphan["text"]
    assert orphan["extract_status"] == "partial"
    assert orphan["subset"] == "commercial"


def test_chembl_tarball_extraction(tmp_path):
    """Committed regression coverage for `_resolve_sqlite_db`'s tarball path.

    A real ChEMBL download is a `chembl_NN_sqlite.tar.gz` tarball -- the ONLY
    shape a real download ever actually produces, and the one carrying the
    `filter="data"` safe-extraction control -- never a bare `.db` (that shape
    is only the unit-test fixture / a manually-placed real file). This was
    previously verified only via an uncommitted, one-off synthetic check
    during implementation (see task-5-report.md Sec 2), which proves it
    worked once, not that a future refactor can't silently drop
    `filter="data"` or break the nested-path glob. Builds a real `.tar.gz`
    wrapping a copy of the `sample.db` fixture under a
    `chembl_37/chembl_37_sqlite/chembl_37.db` internal path, matching the
    real release's known layout.
    """
    import shutil
    import tarfile

    raw_dir = tmp_path / "raw"
    nested = raw_dir / "chembl_37" / "chembl_37_sqlite"
    nested.mkdir(parents=True)
    shutil.copy(FX / "sample.db", nested / "chembl_37.db")

    tar_path = raw_dir / "chembl_37_sqlite.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(raw_dir / "chembl_37", arcname="chembl_37")
    # Only the tarball itself should be a discoverable unit of work -- drop
    # the loose nested tree so serialize_chembl sees exactly one input file
    # (discover_chembl_files's "*.db" pattern would otherwise also match the
    # inner chembl_37.db via rglob).
    shutil.rmtree(raw_dir / "chembl_37")

    processed_dir = tmp_path / "processed"
    res = serialize_chembl(raw_dir, processed_dir)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["failed"] == 0
    assert res["rows"] == 4

    df = _read_shard(processed_dir / "staging" / "chembl")
    rows = df.to_dicts()
    assert rows
    assert all(row["id"].startswith("chembl:") for row in rows)
    # source_file must be the TARBALL's basename, not the extracted inner
    # .db's name -- the unit of work is the release file as it lands in
    # raw_dir, not whatever DuckDB happened to read it from internally.
    assert all(row["source_file"] == "chembl_37_sqlite.tar.gz" for row in rows)

    # Also exercise iter_rows_from_file directly against the tarball (the
    # brief's own named function), independent of the process_one/markers
    # plumbing serialize_chembl() wraps it in above.
    direct_rows = list(iter_rows_from_file(tar_path))
    assert len(direct_rows) == 4
    assert all(r["source_file"] == "chembl_37_sqlite.tar.gz" for r in direct_rows)
    assert {r["id"] for r in direct_rows} == {row["id"] for row in rows}


def test_chembl_serialize_is_idempotent_without_force(tmp_path):
    res1 = serialize_chembl(FX, tmp_path)
    assert res1["ok"] == 1
    # Second run without --force must skip (success marker present).
    res2 = serialize_chembl(FX, tmp_path)
    assert res2["inputs"] == 1
    assert res2["ok"] == 0  # skipped, not re-ok'd
    assert res2["failed"] == 0

    res3 = serialize_chembl(FX, tmp_path, force=True)
    assert res3["ok"] == 1


def test_chembl_report_writes_nothing(tmp_path, capsys):
    rc = main(["--raw-dir", str(FX), "--processed-dir", str(tmp_path), "--report"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "extract_status" in out
    assert not (tmp_path / "staging").exists()
    assert not (tmp_path / "_ops").exists()


def test_chembl_report_respects_max_files(tmp_path, capsys):
    import shutil

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    shutil.copy(FX / "sample.db", raw_dir / "sample.db")
    shutil.copy(FX / "sample.db", raw_dir / "sample_2.db")

    rc = main(
        [
            "--raw-dir",
            str(raw_dir),
            "--processed-dir",
            str(tmp_path),
            "--report",
            "--max-files",
            "1",
        ]
    )
    assert rc == 0
    capped_out = capsys.readouterr().out

    rc = main(["--raw-dir", str(raw_dir), "--processed-dir", str(tmp_path), "--report"])
    assert rc == 0
    uncapped_out = capsys.readouterr().out

    assert "files=1  rows=" in capped_out
    assert "files=2  rows=" in uncapped_out
