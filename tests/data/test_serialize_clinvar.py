"""SP4 Task 8 -- clinvar structured serializer.

Fixture: tests/fixtures/sp4/clinvar/sample.tsv -- 7 lines (1 header + 6 data
rows) extracted VERBATIM (no hand-editing) from a real, fully-downloaded
``variant_summary.txt.gz`` (confirmed by running
``scripts/data/run_pipeline.sh clinvar download --max-files 1`` for real,
then fetching ``tab_delimited/variant_summary.txt.gz`` directly in full --
443,063,763 bytes, matching the server's own ``Content-Length`` -- at
implementation time, 2026-09-16). Column header line confirmed against the
real file's first line, NOT assumed from the README (which has a stale
``PhenotypeIDs`` spelling -- the real header is ``PhenotypeIDS``, capital S,
and starts with ``#AlleleID`` including the leading ``#``; both confirmed by
inspecting the real gzip-decompressed first line).

3 distinct VariationIDs, each carrying its real, naturally-occurring
GRCh37+GRCh38 assembly-duplicate row pair (see serialize_clinvar.py's module
docstring for the full "why" of the assembly-dedup this exercises):
  * VariationID=2 (AlleleID 15041) -- well-populated: long HGVS ``Name``,
    ``ClinicalSignificance="Pathogenic/Likely pathogenic"``, a 3-RCV
    pipe-separated ``RCVaccession``, and a ``PhenotypeList`` mixing two real
    named conditions with a literal ``"not provided"`` entry (ClinVar's own
    real value, not a missing-value sentinel -- kept verbatim, not
    stripped).
  * VariationID=487086 (AlleleID 21136) -- ``PhenotypeList="not provided"``
    (the ENTIRE field, ClinVar's real value when no condition name was
    submitted), short HGVS ``Name``, single RCV -- the sparse case the task
    brief asks to check against MIN_OK_TEXT_LEN.
  * VariationID=4507303 (AlleleID 24289) -- ``ClinicalSignificance="-"``
    (ClinVar's real missing-value sentinel character, confirmed used
    throughout the file for absent values) -- exercises the
    sentinel-to-``None`` normalization (NULLIF in the DuckDB query), and the
    "clause omitted, not fabricated" path in ``_build_text``.

Confirmed empirically across the FULL real file (9,056,310 data rows) before
building this fixture: for every VariationID with >1 physical row, Name /
ClinicalSignificance / PhenotypeIDs / PhenotypeList are byte-identical across
its assembly-duplicate rows (zero mismatches found) -- the fields this
serializer's ``text`` template uses are assembly-invariant, so picking any
one assembly row per VariationID (this module prefers GRCh38 > GRCh37 >
NCBI36 > other) never discards real per-assembly TEXT content, only
per-assembly genomic coordinates this task's row shape does not carry.

All `not pg` -- no DB.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from episteme.data import article_schema
from episteme.data.clinvar.serialize_clinvar import (
    iter_rows_from_file,
    main,
    serialize_clinvar,
)

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp4" / "clinvar"


def _read_shard(staging_dir: Path) -> pl.DataFrame:
    shards = list(staging_dir.glob("*.parquet")) or list(staging_dir.glob("*.jsonl"))
    assert shards, f"no shard under {staging_dir}"
    s = shards[0]
    return pl.read_parquet(s) if s.suffix == ".parquet" else pl.read_ndjson(s)


def test_clinvar_serialize_rows(tmp_path):
    res = serialize_clinvar(FX, tmp_path)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["failed"] == 0
    # 6 physical TSV data rows (3 VariationIDs x GRCh37+GRCh38) dedup to 3.
    assert res["rows"] == 3

    df = _read_shard(tmp_path / "staging" / "clinvar")
    rows = df.to_dicts()

    assert {row["source"] for row in rows} == {"clinvar"}
    assert all(row["id"].startswith("clinvar:") for row in rows)
    assert len({row["id"] for row in rows}) == 3
    assert {row["id"] for row in rows} == {
        "clinvar:2",
        "clinvar:487086",
        "clinvar:4507303",
    }

    # Source-anchored governance override (SP4.1 Task 11): public_domain ->
    # commercial; license_raw keeps the real disclaimer/data-use text.
    assert all(row["license"] == "public_domain" for row in rows)
    assert all(row["subset"] == "commercial" for row in rows)
    assert all("not intended for direct diagnostic use" in row["license_raw"] for row in rows)

    assert all(row["container_id"] is None for row in rows)
    assert all(row["book_meta"] is None for row in rows)
    # ClinVar variant records carry no bibliographic shape.
    assert all(row["title"] is None for row in rows)
    assert all(row["journal"] is None for row in rows)
    assert all(row["year"] is None for row in rows)
    assert all(row["authors"] is None for row in rows)
    assert all(row["pmid"] is None and row["pmcid"] is None and row["doi"] is None for row in rows)
    assert all(row["extract_status"] in article_schema.EXTRACT_STATUSES for row in rows)

    by_id = {row["id"]: row for row in rows}

    v2 = by_id["clinvar:2"]
    assert "NM_014855.3(AP5Z1)" in v2["text"]
    assert "Pathogenic/Likely pathogenic" in v2["text"]
    assert "Hereditary spastic paraplegia 48" in v2["text"]
    assert "not provided" in v2["text"]  # real PhenotypeList entry, kept verbatim
    assert "RCV000000012" in v2["text"]
    # Well-populated, long HGVS name + 3-condition phenotype list -> clears
    # MIN_OK_TEXT_LEN comfortably.
    assert v2["extract_status"] == "ok"

    v487086 = by_id["clinvar:487086"]
    assert "NM_005413.4(SIX3)" in v487086["text"]
    assert "Pathogenic" in v487086["text"]
    assert "not provided" in v487086["text"]
    assert "RCV002263814" in v487086["text"]
    # Sparse: PhenotypeList is JUST "not provided", short HGVS name, one
    # RCV -- honest low-confidence "partial", not a bug (mirrors chembl/
    # pubchem's near-boundary findings for compact structured records).
    assert v487086["extract_status"] == "partial"

    v4507303 = by_id["clinvar:4507303"]
    assert "NM_000534.5(PMS1)" in v4507303["text"]
    # ClinicalSignificance is ClinVar's real "-" missing-value sentinel for
    # this record -- must be omitted from the sentence, NOT rendered
    # literally as "is classified -" and NOT fabricated as "unknown".
    assert "is classified -" not in v4507303["text"]
    assert "Thyroid cancer, nonmedullary, 1" in v4507303["text"]
    assert "RCV006220035" in v4507303["text"]


def test_clinvar_report_writes_nothing(tmp_path, capsys):
    rc = main(["--raw-dir", str(FX), "--processed-dir", str(tmp_path), "--report"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "extract_status" in out
    assert not (tmp_path / "staging").exists()
    assert not (tmp_path / "_ops").exists()


def test_clinvar_report_respects_max_files(tmp_path, capsys):
    import shutil

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    shutil.copy(FX / "sample.tsv", raw_dir / "sample.tsv")
    shutil.copy(FX / "sample.tsv", raw_dir / "sample_2.tsv")

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


def test_clinvar_serialize_is_idempotent_without_force(tmp_path):
    res1 = serialize_clinvar(FX, tmp_path)
    assert res1["ok"] == 1
    # Second run without --force must skip (success marker present).
    res2 = serialize_clinvar(FX, tmp_path)
    assert res2["inputs"] == 1
    assert res2["ok"] == 0  # skipped, not re-ok'd
    assert res2["failed"] == 0

    res3 = serialize_clinvar(FX, tmp_path, force=True)
    assert res3["ok"] == 1


def test_clinvar_gzip_input_is_supported(tmp_path):
    """The real download_clinvar.sh artifact is
    ``tab_delimited/variant_summary.txt.gz`` -- confirm gzip-compressed
    input parses identically to the bare fixture (DuckDB's CSV reader
    transparently decompresses ``.gz`` -- same idiom as
    serialize_pubchem.py, no separate ``gzip.open`` pre-decompress step
    needed for a TSV-shaped source)."""
    import gzip
    import shutil

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    src = FX / "sample.tsv"
    dst = raw_dir / "variant_summary.txt.gz"
    with src.open("rb") as fsrc, gzip.open(dst, "wb") as fdst:
        shutil.copyfileobj(fsrc, fdst)

    processed_dir = tmp_path / "processed"
    res = serialize_clinvar(raw_dir, processed_dir)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["rows"] == 3

    df = _read_shard(processed_dir / "staging" / "clinvar")
    rows = df.to_dicts()
    assert {row["id"] for row in rows} == {"clinvar:2", "clinvar:487086", "clinvar:4507303"}


def test_clinvar_assembly_dedup_prefers_grch38():
    """Directly exercises the module's ``_QUERY`` (not just its
    downstream ``episteme.articles`` row, which doesn't carry ``assembly``
    at all -- ``clinvar_row``/``_build_text`` never read it, so a row-level
    assertion alone cannot prove which assembly the QUALIFY clause actually
    picked). 3 distinct VariationIDs in, exactly 3 rows out -- the real
    GRCh37+GRCh38 assembly-duplicate pairs (6 physical TSV lines) collapse
    to one row per VariationID, and the SURVIVING row for each is
    confirmed to be the real GRCh38 physical line, not merely "some" row
    (a reversed or deleted ``CASE Assembly ...`` would still dedup to 3
    rows but change which one survives -- this assertion would catch
    that, a row-count-only assertion would not)."""
    import duckdb

    from episteme.data.clinvar.serialize_clinvar import _QUERY

    con = duckdb.connect()
    try:
        con.execute(_QUERY, [str(FX / "sample.tsv")])
        cols = [d[0] for d in con.description]
        rows = [dict(zip(cols, raw, strict=True)) for raw in con.fetchall()]
    finally:
        con.close()

    assert len(rows) == 3
    by_vid = {r["variation_id"]: r for r in rows}
    assert set(by_vid) == {2, 487086, 4507303}
    assert all(r["assembly"] == "GRCh38" for r in rows)

    # Also exercise iter_rows_from_file directly (the brief's own named
    # function) for the downstream row shape.
    downstream = list(iter_rows_from_file(FX / "sample.tsv"))
    assert len(downstream) == 3
    assert {r["source_record_id"] for r in downstream} == {"2", "487086", "4507303"}


def test_clinvar_record_without_variation_id_is_counted_and_skipped(tmp_path):
    import json

    raw = tmp_path / "raw"
    raw.mkdir()
    src = (FX / "sample.tsv").read_text(encoding="utf-8").splitlines()
    header, first = src[0], src[1]
    cols = header.lstrip("#").split("\t")
    bad = first.split("\t")
    bad[cols.index("VariationID")] = ""
    (raw / "variant_summary.txt").write_text(
        "\n".join([header, first, "\t".join(bad)]) + "\n", encoding="utf-8"
    )
    res = serialize_clinvar(raw, tmp_path / "processed")
    assert res["ok"] == 1 and res["failed"] == 0
    df = pl.read_parquet(next((tmp_path / "processed" / "staging" / "clinvar").glob("*.parquet")))
    ids = [str(i) for i in df["id"].to_list()]
    assert not any(i.endswith(":unknown") for i in ids)
    assert len(ids) == 1 and ids[0].startswith("clinvar:")
    marker = json.loads(
        next((tmp_path / "processed" / "_ops" / "clinvar" / "success").glob("*.ok")).read_text()
    )
    assert marker["stats"]["skipped_no_id"] == 1
