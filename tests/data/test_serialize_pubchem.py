"""SP4 Task 7 -- pubchem structured serializer.

Fixture: tests/fixtures/sp4/pubchem/ -- real per-CID rows extracted verbatim
from real ``download_pubchem.sh compound_extras`` output (confirmed by
running ``scripts/data/run_pipeline.sh pubchem download --max-files 3`` for
real, and by fetching ``Compound/Extras/README-Extras`` directly for the
column-shape docs; both at implementation time, 2026-09-16). Four files,
matching the REAL PubChem ``Compound/Extras`` per-property-file shape (NOT
one joined file -- see serialize_pubchem.py's module docstring for the full
"why" and the two-option decision):
  * CID-SMILES.tsv (PRIMARY, drives iteration) -- CIDs 1, 4, 406.
  * CID-IUPAC.tsv (sibling) -- CIDs 1, 4 only. CID 406 deliberately
    missing -- a REAL, observed gap (confirmed against a larger real sample:
    CID-SMILES.gz has materially more rows than CID-IUPAC.gz in the same
    byte range), not a fabricated one. Exercises the best-effort-join-miss
    path.
  * CID-Title.tsv (sibling) -- CIDs 1, 4, 406 (PubChem's own title fallback
    for 406 is literally the string "CID 406" -- real data, kept verbatim).
  * CID-Mass.tsv (sibling) -- CIDs 1, 4, 406.

CID 1 (Acetyl-DL-carnitine) is a well-populated, long-identifier record
whose joined text comfortably clears MIN_OK_TEXT_LEN (200 chars) ->
extract_status="ok". CID 4 (1-Amino-2-propanol) is ALSO fully joined (all
4 fields present) but has short identifiers -- its text lands at ~187
chars, UNDER the threshold -> extract_status="partial" despite being
fully-populated (mirrors chembl task-5's near-boundary finding, confirmed
for pubchem too, see task-7-report.md). CID 406 is missing IUPAC (join
miss) and is also short -> "partial".

All `not pg` -- no DB.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from episteme.data import article_schema
from episteme.data.pubchem.serialize_pubchem import (
    iter_rows_from_file,
    main,
    serialize_pubchem,
)

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp4" / "pubchem"


def _read_shard(staging_dir: Path) -> pl.DataFrame:
    shards = list(staging_dir.glob("*.parquet")) or list(staging_dir.glob("*.jsonl"))
    assert shards, f"no shard under {staging_dir}"
    s = shards[0]
    return pl.read_parquet(s) if s.suffix == ".parquet" else pl.read_ndjson(s)


def test_pubchem_serialize_rows(tmp_path):
    res = serialize_pubchem(FX, tmp_path)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["failed"] == 0
    assert res["rows"] == 3

    df = _read_shard(tmp_path / "staging" / "pubchem")
    rows = df.to_dicts()

    assert {row["source"] for row in rows} == {"pubchem"}
    assert all(row["id"].startswith("pubchem:") for row in rows)
    assert len({row["id"] for row in rows}) == 3
    assert {row["id"] for row in rows} == {"pubchem:1", "pubchem:4", "pubchem:406"}

    # Source-anchored governance override (SP4.1 Task 11): public_domain ->
    # commercial; license_raw keeps the real Fair Use Disclaimer text (which
    # does not itself contain the words "Fair Use"; license_raw is capped at 300 chars).
    assert all(row["license"] == "public_domain" for row in rows)
    assert all(row["subset"] == "commercial" for row in rows)
    assert all(
        "Databases of molecular data on the NCBI FTP site" in row["license_raw"] for row in rows
    )

    assert all(row["container_id"] is None for row in rows)
    assert all(row["book_meta"] is None for row in rows)
    # PubChem compound_extras records carry no bibliographic shape.
    assert all(row["title"] is None for row in rows)
    assert all(row["journal"] is None for row in rows)
    assert all(row["year"] is None for row in rows)
    assert all(row["authors"] is None for row in rows)
    assert all(row["pmid"] is None and row["pmcid"] is None and row["doi"] is None for row in rows)
    assert all(row["extract_status"] in article_schema.EXTRACT_STATUSES for row in rows)

    by_id = {row["id"]: row for row in rows}

    cid1 = by_id["pubchem:1"]
    assert "Acetyl-DL-carnitine" in cid1["text"]
    assert "3-acetyloxy-4-(trimethylazaniumyl)butanoate" in cid1["text"]
    assert "C9H17NO4" in cid1["text"]
    assert "CC(=O)OC(CC(=O)[O-])C[N+](C)(C)C" in cid1["text"]
    # Well-populated + long identifiers -> comfortably clears MIN_OK_TEXT_LEN.
    assert cid1["extract_status"] == "ok"

    cid4 = by_id["pubchem:4"]
    assert "1-Amino-2-propanol" in cid4["text"]
    assert "1-aminopropan-2-ol" in cid4["text"]
    assert "C3H9NO" in cid4["text"]
    assert "CC(CN)O" in cid4["text"]
    # Fully joined (all 4 fields present) but short identifiers -> still
    # legitimately lands "partial", not "ok" -- honest, not a bug.
    assert cid4["extract_status"] == "partial"

    cid406 = by_id["pubchem:406"]
    assert "CID 406" in cid406["text"]  # PubChem's own title fallback
    assert "HO3P-2" in cid406["text"]
    assert "[O-]P(=O)[O-]" in cid406["text"]
    # IUPAC genuinely missing (real join-miss) -- must not raise, must not
    # fabricate a name; simply omitted from the sentence.
    assert "IUPAC name" not in cid406["text"]
    assert cid406["extract_status"] == "partial"


def test_pubchem_report_writes_nothing(tmp_path, capsys):
    rc = main(["--raw-dir", str(FX), "--processed-dir", str(tmp_path), "--report"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "extract_status" in out
    assert not (tmp_path / "staging").exists()
    assert not (tmp_path / "_ops").exists()


def test_pubchem_report_respects_max_files(tmp_path, capsys):
    import shutil

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    # Distinct basenames (not two same-named files in different dirs) --
    # list_input_files dedups by basename (it doubles as the marker
    # identity), and a real raw_dir only ever holds one canonically-named
    # CID-SMILES.gz anyway. Mirrors chembl/uniprot's own multi-file
    # --max-files test idiom.
    shutil.copy(FX / "CID-SMILES.tsv", raw_dir / "CID-SMILES.tsv")
    shutil.copy(FX / "CID-SMILES.tsv", raw_dir / "CID-SMILES_2.tsv")

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

    assert "files=1  rows=3" in capped_out
    assert "files=2  rows=6" in uncapped_out


def test_pubchem_serialize_is_idempotent_without_force(tmp_path):
    res1 = serialize_pubchem(FX, tmp_path)
    assert res1["ok"] == 1
    # Second run without --force must skip (success marker present).
    res2 = serialize_pubchem(FX, tmp_path)
    assert res2["inputs"] == 1
    assert res2["ok"] == 0  # skipped, not re-ok'd
    assert res2["failed"] == 0

    res3 = serialize_pubchem(FX, tmp_path, force=True)
    assert res3["ok"] == 1


def test_pubchem_gzip_input_is_supported(tmp_path):
    """The real download_pubchem.sh artifact is bare-``.gz`` (e.g.
    ``CID-SMILES.gz``, NOT ``.tsv.gz``) -- confirm gzip-compressed input
    (both primary AND siblings) parses identically to the bare fixture."""
    import gzip
    import shutil

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    for stem in ("CID-SMILES", "CID-IUPAC", "CID-Title", "CID-Mass"):
        src = FX / f"{stem}.tsv"
        dst = raw_dir / f"{stem}.gz"
        with src.open("rb") as fsrc, gzip.open(dst, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst)

    processed_dir = tmp_path / "processed"
    res = serialize_pubchem(raw_dir, processed_dir)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["rows"] == 3

    df = _read_shard(processed_dir / "staging" / "pubchem")
    rows = df.to_dicts()
    assert {row["id"] for row in rows} == {"pubchem:1", "pubchem:4", "pubchem:406"}
    by_id = {row["id"]: row for row in rows}
    assert "Acetyl-DL-carnitine" in by_id["pubchem:1"]["text"]
    assert by_id["pubchem:1"]["extract_status"] == "ok"


def test_pubchem_primary_file_without_any_siblings(tmp_path):
    """A REAL observed scenario (this task's own real end-to-end download,
    ``run_pipeline.sh pubchem download --max-files 3``, fetched
    CID-Biologics.tsv.gz + CID-Component.gz -- NOT CID-SMILES/IUPAC/Title/
    Mass): a primary file can legitimately land in raw_dir with zero
    sibling property files present. Must not raise -- must serialize sparser
    (SMILES-only) rows, most of which legitimately land "partial" (a small
    molecule's bare SMILES sentence is well under MIN_OK_TEXT_LEN)."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    import shutil

    shutil.copy(FX / "CID-SMILES.tsv", raw_dir / "CID-SMILES.tsv")

    rows = list(iter_rows_from_file(raw_dir / "CID-SMILES.tsv"))
    assert len(rows) == 3
    by_id = {r["id"]: r for r in rows}
    assert by_id["pubchem:1"]["text"] == (
        "PubChem compound CID 1 has canonical SMILES "
        "CC(=O)OC(CC(=O)[O-])C[N+](C)(C)C, as catalogued in the NCBI "
        "PubChem Compound database."
    )
    # No siblings at all -> every optional field omitted, no exception.
    assert all(row["extract_status"] in article_schema.EXTRACT_STATUSES for row in rows)


def test_pubchem_record_without_cid_is_counted_and_skipped(tmp_path):
    import json

    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "CID-SMILES.tsv").write_text("1\tCC(=O)O\n\tCCO\n", encoding="utf-8")
    res = serialize_pubchem(raw, tmp_path / "processed")
    assert res["ok"] == 1 and res["failed"] == 0
    df = pl.read_parquet(next((tmp_path / "processed" / "staging" / "pubchem").glob("*.parquet")))
    ids = [str(i) for i in df["id"].to_list()]
    assert not any(i.endswith(":unknown") for i in ids)
    assert ids == ["pubchem:1"]
    marker = json.loads(
        next((tmp_path / "processed" / "_ops" / "pubchem" / "success").glob("*.ok")).read_text()
    )
    assert marker["stats"]["skipped_no_id"] == 1
