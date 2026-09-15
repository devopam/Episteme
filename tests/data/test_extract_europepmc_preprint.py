"""SP2 Task 7 — europepmc_preprint extractor, reconciled to the extract_pubmed
shape (per-ID ``PPR*.xml`` discovery; §4.7 CLI).

Fixtures: tests/fixtures/sp2/europepmc_preprint/
  PPR1.xml — valid JATS <article> with <front> metadata, a >300-char <body>,
             and a <permissions><license> carrying "CC BY 4.0" -> ok / commercial.
  PPR2.xml — metadata-only (doi + author + year, no title/abstract/body)
             -> extract_status "empty".

All `not pg` — no DB (the best-effort audit degrades to the JSONL mirror).
"""

from __future__ import annotations

from pathlib import Path

from episteme.data import article_schema
from episteme.data.europepmc.preprints.extract_europepmc_preprints import (
    extract_europepmc_preprints,
    main,
)

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp2" / "europepmc_preprint"


def _read_shard(staging_dir: Path):
    import polars as pl

    shards = list(staging_dir.glob("*.parquet")) or list(staging_dir.glob("*.jsonl"))
    assert shards, f"no shard under {staging_dir}"
    s = shards[0]
    return pl.read_parquet(s) if s.suffix == ".parquet" else pl.read_ndjson(s)


def _all_rows(staging_dir: Path):
    import polars as pl

    shards = list(staging_dir.glob("*.parquet")) or list(staging_dir.glob("*.jsonl"))
    out = []
    for s in shards:
        df = pl.read_parquet(s) if s.suffix == ".parquet" else pl.read_ndjson(s)
        out.extend(df.to_dicts())
    return out


def test_preprint_extract_rows(tmp_path):
    res = extract_europepmc_preprints(FX, tmp_path)
    assert res["inputs"] == 2
    assert res["failed"] == 0
    assert res["rows"] >= 1

    rows = _all_rows(tmp_path / "staging" / "europepmc_preprint")
    assert {r["source"] for r in rows} == {"europepmc_preprint"}
    assert all(r["extract_status"] in article_schema.EXTRACT_STATUSES for r in rows)
    assert all(r["container_id"] is None for r in rows)
    assert all(r["book_meta"] is None for r in rows)
    assert all(r["mesh"] is None for r in rows)
    # source_record_id is the PPR{n} file stem
    assert {r["source_record_id"] for r in rows} == {"PPR1", "PPR2"}

    ppr1 = next(r for r in rows if r["source_record_id"] == "PPR1")
    assert ppr1["extract_status"] == "ok"
    assert ppr1["body_text"]
    assert ppr1["doi"] == "10.1101/2026.09.01.123456"
    assert ppr1["pmid"] is None
    assert ppr1["subset"] == "commercial"  # CC BY 4.0
    assert ppr1["license"] == "CC BY"
    assert ppr1["authors"] and "Ada Okafor" in ppr1["authors"]


def test_preprint_metadata_only_is_empty(tmp_path):
    extract_europepmc_preprints(FX, tmp_path)
    rows = _all_rows(tmp_path / "staging" / "europepmc_preprint")
    ppr2 = next(r for r in rows if r["source_record_id"] == "PPR2")
    assert ppr2["body_text"] is None
    assert ppr2["extract_status"] == "empty"


def test_preprint_report_writes_nothing(tmp_path, capsys):
    rc = main(["--raw-dir", str(FX), "--processed-dir", str(tmp_path), "--report"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "extract_status" in out  # a field-shape table was printed
    assert not (tmp_path / "staging").exists()
    assert not (tmp_path / "_ops").exists()
