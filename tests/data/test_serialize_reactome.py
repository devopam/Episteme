"""SP4 Task 9 -- reactome structured serializer.

Fixture: tests/fixtures/sp4/reactome/ -- real rows extracted verbatim from
Reactome's real current-release flat directory (fetched directly at
implementation time, 2026-09-16/17 -- NOT via ``--max-files N``, which
proved impractical: the real listing is alphabetically ordered and a bounded
run burns its whole budget on ``ChEBI*``/``Ensembl*`` files without ever
reaching ``R``, confirmed by an actual run that fetched ~800MB and never got
past "E"). Three files, matching serialize_reactome.py's real multi-file
unit-of-work decision (see its module docstring for the full "why"):

  * ReactomePathways.tsv (PRIMARY) -- 5 real rows, deliberately chosen as
    two matched human/bovine pairs plus one extra human row, to exercise
    every enrichment-presence combination with REAL (not fabricated) data:
      - R-HSA-164843 "2-LTR circle formation" (Homo sapiens) -- summation
        present + 13 real UniProt hits (capped sample of 5 + "among 13
        annotated in total").
      - R-HSA-166016 "Toll Like Receptor 4 (TLR4) Cascade" (Homo sapiens)
        -- summation present (as TWO real duplicate rows in
        pathway2summation.tsv, see below) + 18 real UniProt hits.
      - R-HSA-109581 "Apoptosis" (Homo sapiens) -- summation present, ZERO
        UniProt hits (a real, observed case: top-level/umbrella pathways
        carry no DIRECT protein annotation in the plain, lowest-level-only
        UniProt2Reactome.txt -- see module docstring's file-selection
        reasoning). Exercises the protein-join-miss path.
      - R-BTA-1059683 "Interleukin-6 signaling" (Bos taurus) -- NO
        summation (pathway2summation.txt is human-only by construction,
        confirmed empirically: its 2,883 distinct Identifiers are EXACTLY
        the 2,883 Homo sapiens rows of the real ReactomePathways.txt) + 11
        real UniProt hits. The real bovine/human name-twin of
        R-HSA-1059683-shaped pathways -- not fabricated, Reactome
        genuinely re-annotates orthologous pathways per species.
      - R-BTA-109581 "Apoptosis" (Bos taurus) -- the bovine twin of
        R-HSA-109581: NO summation, ZERO UniProt hits. The fully-sparse
        case.

  * pathway2summation.tsv (SIBLING 1) -- header + 4 real data rows (one
    pathway_id, R-HSA-166016, appears TWICE with two genuinely different
    summation paragraphs -- a real duplicate-id finding, not injected for
    the test; exercises the module's QUALIFY-based dedup, alphabetical-
    first-summation tiebreak, mirroring clinvar task-8's assembly-dedup
    precedent).

  * UniProt2Reactome.tsv (SIBLING 2) -- 42 real rows (13 + 18 + 11 UniProt
    accessions for R-HSA-164843 / R-HSA-166016 / R-BTA-1059683
    respectively), no header (matches the real file's real shape).

All `not pg` -- no DB.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from episteme.data import article_schema
from episteme.data.reactome.serialize_reactome import (
    iter_rows_from_file,
    main,
    serialize_reactome,
)

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp4" / "reactome"


def _read_shard(staging_dir: Path) -> pl.DataFrame:
    shards = list(staging_dir.glob("*.parquet")) or list(staging_dir.glob("*.jsonl"))
    assert shards, f"no shard under {staging_dir}"
    s = shards[0]
    return pl.read_parquet(s) if s.suffix == ".parquet" else pl.read_ndjson(s)


def test_reactome_serialize_rows(tmp_path):
    res = serialize_reactome(FX, tmp_path)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["failed"] == 0
    assert res["rows"] == 5

    df = _read_shard(tmp_path / "staging" / "reactome")
    rows = df.to_dicts()

    assert {row["source"] for row in rows} == {"reactome"}
    assert all(row["id"].startswith("reactome:") for row in rows)
    assert len({row["id"] for row in rows}) == 5
    assert {row["id"] for row in rows} == {
        "reactome:R-HSA-164843",
        "reactome:R-HSA-166016",
        "reactome:R-HSA-109581",
        "reactome:R-BTA-1059683",
        "reactome:R-BTA-109581",
    }

    # Reactome's real License Agreement page, Section 1c ("Data"), fetched
    # directly -- contains the literal "CC0" token, hits normalize_license's
    # existing CC0 arm exactly as the brief predicted (unlike pubchem/
    # clinvar's flagged licence gaps -- see module docstring).
    assert all(row["license"] == "CC0" for row in rows)
    assert all(row["subset"] == "commercial" for row in rows)
    assert all(row["license_raw"] is not None for row in rows)

    assert all(row["container_id"] is None for row in rows)
    assert all(row["book_meta"] is None for row in rows)
    # Reactome pathway records carry no bibliographic shape.
    assert all(row["title"] is None for row in rows)
    assert all(row["journal"] is None for row in rows)
    assert all(row["year"] is None for row in rows)
    assert all(row["authors"] is None for row in rows)
    assert all(row["pmid"] is None and row["pmcid"] is None and row["doi"] is None for row in rows)
    assert all(row["extract_status"] in article_schema.EXTRACT_STATUSES for row in rows)

    by_id = {row["id"]: row for row in rows}

    hsa_164843 = by_id["reactome:R-HSA-164843"]
    assert "2-LTR circle formation" in hsa_164843["text"]
    assert "Homo sapiens" in hsa_164843["text"]
    assert "non-homologous DNA end-joining" in hsa_164843["text"]  # real summation prose
    assert "O75475" in hsa_164843["text"]  # first (alphabetical) capped protein sample
    assert "among 13 annotated in total" in hsa_164843["text"]
    # Rich real summation text -> comfortably clears MIN_OK_TEXT_LEN.
    assert hsa_164843["extract_status"] == "ok"

    hsa_166016 = by_id["reactome:R-HSA-166016"]
    # Dedup must pick exactly ONE of the two real duplicate summation rows
    # (alphabetical-first via QUALIFY ROW_NUMBER ORDER BY summation) -- not
    # both, not neither.
    assert "TLR4 is unique among the TLR family" in hsa_166016["text"]
    assert "Toll-like Receptor 4 is a microbe associated" not in hsa_166016["text"]
    assert "among 18 annotated in total" in hsa_166016["text"]
    assert hsa_166016["extract_status"] == "ok"

    hsa_109581 = by_id["reactome:R-HSA-109581"]
    assert "Apoptosis is a distinct form of cell death" in hsa_109581["text"]
    # Real protein-join-miss: a top-level/umbrella pathway with summation
    # but ZERO direct UniProt annotations in the plain (lowest-level-only)
    # mapping file -- must not raise, must simply omit the protein clause.
    assert "Known protein participants" not in hsa_109581["text"]
    assert hsa_109581["extract_status"] == "ok"

    bta_1059683 = by_id["reactome:R-BTA-1059683"]
    assert "Interleukin-6 signaling" in bta_1059683["text"]
    assert "Bos taurus" in bta_1059683["text"]
    # Non-human: summation-less BY CONSTRUCTION (pathway2summation.txt is
    # human-only), must not fabricate a description -- just the subject
    # sentence + protein enrichment.
    assert "A0A3Q1LN00" in bta_1059683["text"]
    assert "among 11 annotated in total" in bta_1059683["text"]

    bta_109581 = by_id["reactome:R-BTA-109581"]
    assert bta_109581["text"] == (
        "Reactome pathway Apoptosis (Bos taurus). As catalogued in the Reactome pathway database."
    )
    # Fully sparse (no summation, no protein hits) -> short text, honest
    # "partial", not a bug -- mirrors every prior SP4 structured source's
    # near-boundary finding for compact/sparse records. Never "empty": the
    # subject sentence is always non-empty.
    assert bta_109581["extract_status"] == "partial"


def test_reactome_report_writes_nothing(tmp_path, capsys):
    rc = main(["--raw-dir", str(FX), "--processed-dir", str(tmp_path), "--report"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "extract_status" in out
    assert not (tmp_path / "staging").exists()
    assert not (tmp_path / "_ops").exists()


def test_reactome_report_respects_max_files(tmp_path, capsys):
    import shutil

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    # Only the PRIMARY file is duplicated -- siblings stay put (matches the
    # real shape: a real raw_dir holds exactly one ReactomePathways.txt, and
    # this exercises --max-files capping the primary set, same idiom as
    # pubchem/clinvar's own multi-file --max-files test).
    shutil.copy(FX / "ReactomePathways.tsv", raw_dir / "ReactomePathways.tsv")
    shutil.copy(FX / "ReactomePathways.tsv", raw_dir / "ReactomePathways_2.tsv")
    shutil.copy(FX / "pathway2summation.tsv", raw_dir / "pathway2summation.tsv")
    shutil.copy(FX / "UniProt2Reactome.tsv", raw_dir / "UniProt2Reactome.tsv")

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

    assert "files=1  rows=5" in capped_out
    assert "files=2  rows=10" in uncapped_out


def test_reactome_serialize_is_idempotent_without_force(tmp_path):
    res1 = serialize_reactome(FX, tmp_path)
    assert res1["ok"] == 1
    # Second run without --force must skip (success marker present).
    res2 = serialize_reactome(FX, tmp_path)
    assert res2["inputs"] == 1
    assert res2["ok"] == 0  # skipped, not re-ok'd
    assert res2["failed"] == 0

    res3 = serialize_reactome(FX, tmp_path, force=True)
    assert res3["ok"] == 1


def test_reactome_gzip_input_is_supported(tmp_path):
    """A real download_reactome.sh artifact could plausibly land as
    ``.txt.gz`` (the download script's own manifest regex accepts
    ``\\.(txt|...)(\\.gz)?$``) -- confirm gzip-compressed primary input
    parses identically to the bare fixture. Siblings stay bare .tsv (best-
    effort discovery checks both compressed/uncompressed candidates
    independently per file, same idiom as pubchem/clinvar)."""
    import gzip
    import shutil

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    src = FX / "ReactomePathways.tsv"
    dst = raw_dir / "ReactomePathways.txt.gz"
    with src.open("rb") as fsrc, gzip.open(dst, "wb") as fdst:
        shutil.copyfileobj(fsrc, fdst)
    shutil.copy(FX / "pathway2summation.tsv", raw_dir / "pathway2summation.tsv")
    shutil.copy(FX / "UniProt2Reactome.tsv", raw_dir / "UniProt2Reactome.tsv")

    processed_dir = tmp_path / "processed"
    res = serialize_reactome(raw_dir, processed_dir)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["rows"] == 5

    df = _read_shard(processed_dir / "staging" / "reactome")
    rows = df.to_dicts()
    assert {row["id"] for row in rows} == {
        "reactome:R-HSA-164843",
        "reactome:R-HSA-166016",
        "reactome:R-HSA-109581",
        "reactome:R-BTA-1059683",
        "reactome:R-BTA-109581",
    }
    by_id = {row["id"]: row for row in rows}
    assert "among 13 annotated in total" in by_id["reactome:R-HSA-164843"]["text"]


def test_reactome_primary_file_without_any_siblings(tmp_path):
    """A real observed scenario: a raw_dir might legitimately hold only
    ReactomePathways.txt (e.g. an early --max-files-capped download that
    never reached the alphabetically-later pathway2summation.txt/
    UniProt2Reactome.txt files, confirmed impractical for real in this
    task's own attempt -- see task-9-report.md). Must not raise -- must
    serialize sparser (name+species-only) rows."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    import shutil

    shutil.copy(FX / "ReactomePathways.tsv", raw_dir / "ReactomePathways.tsv")

    rows = list(iter_rows_from_file(raw_dir / "ReactomePathways.tsv"))
    assert len(rows) == 5
    by_id = {r["id"]: r for r in rows}
    assert by_id["reactome:R-HSA-164843"]["text"] == (
        "Reactome pathway 2-LTR circle formation (Homo sapiens). "
        "As catalogued in the Reactome pathway database."
    )
    # No siblings at all -> every row lands "partial" (short subject-only
    # sentence), no exception.
    assert all(row["extract_status"] in article_schema.EXTRACT_STATUSES for row in rows)


def test_reactome_summation_dedup_prefers_first_alphabetical():
    """Directly exercises the module's QUALIFY-based dedup for the one real
    duplicate Identifier (R-HSA-166016) in pathway2summation.tsv -- a
    row-level assertion alone (via iter_rows_from_file) proves only that
    SOME summation survived, not which one or that exactly one row resulted.
    This test proves both: exactly one summation row per pathway_id survives
    the CTE, and it is the alphabetically-first of the two real paragraphs."""
    import duckdb

    from episteme.data.reactome.serialize_reactome import (
        _SUMMATION_COLUMNS,
        _columns_literal,
    )

    con = duckdb.connect()
    try:
        literal = _columns_literal(_SUMMATION_COLUMNS)
        con.execute(
            "SELECT pathway_id, summation FROM read_csv(?, delim='\t', header=true, "
            f"quote='', ignore_errors=true, columns={literal}) "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY pathway_id ORDER BY summation) = 1 "
            "AND pathway_id = 'R-HSA-166016'",
            [str(FX / "pathway2summation.tsv")],
        )
        rows = con.fetchall()
    finally:
        con.close()

    assert len(rows) == 1
    assert rows[0][1].startswith("TLR4 is unique among the TLR family")


def test_reactome_record_without_pathway_id_is_counted_and_skipped(tmp_path):
    import json

    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "ReactomePathways.tsv").write_text(
        "R-HSA-109581\tApoptosis\tHomo sapiens\n\tNameless pathway\tHomo sapiens\n",
        encoding="utf-8",
    )
    res = serialize_reactome(raw, tmp_path / "processed")
    assert res["ok"] == 1 and res["failed"] == 0
    df = pl.read_parquet(next((tmp_path / "processed" / "staging" / "reactome").glob("*.parquet")))
    ids = [str(i) for i in df["id"].to_list()]
    assert not any(i.endswith(":unknown") for i in ids)
    assert ids == ["reactome:R-HSA-109581"]
    marker = json.loads(
        next((tmp_path / "processed" / "_ops" / "reactome" / "success").glob("*.ok")).read_text()
    )
    assert marker["stats"]["skipped_no_id"] == 1
