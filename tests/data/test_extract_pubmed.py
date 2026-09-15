"""SP2 Task 5 — pubmed extractor, reconciled to the extract_pmc shape.

Fixture: tests/fixtures/sp2/pubmed/pubmed_sample.xml.gz — 3 <PubmedArticle>
records: (a) full narrative abstract + MeSH + DOI + year, (b) empty
<ArticleTitle/> and NO <Abstract> -> extract_status="empty", (c) structured
abstract only (Label=METHODS/RESULTS) with year via <MedlineDate>.

All `not pg` — no DB.
"""

from __future__ import annotations

from pathlib import Path

from episteme.data import article_schema
from episteme.data.pubmed.extract_pubmed import extract_pubmed, main

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp2" / "pubmed"


def _read_shard(staging_dir: Path):
    import polars as pl

    shards = list(staging_dir.glob("*.parquet")) or list(staging_dir.glob("*.jsonl"))
    assert shards, f"no shard under {staging_dir}"
    s = shards[0]
    return pl.read_parquet(s) if s.suffix == ".parquet" else pl.read_ndjson(s)


def test_pubmed_extract_rows(tmp_path):
    res = extract_pubmed(FX, tmp_path)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["failed"] == 0
    assert res["rows"] == 3

    df = _read_shard(tmp_path / "staging" / "pubmed")
    rows = df.to_dicts()
    assert {r["source"] for r in rows} == {"pubmed"}
    assert all(r["pmid"] for r in rows)
    assert all(r["extract_status"] in article_schema.EXTRACT_STATUSES for r in rows)
    # container_id / book_meta are None on EVERY pubmed row (non-negotiable)
    assert all(r["container_id"] is None for r in rows)
    assert all(r["book_meta"] is None for r in rows)
    # PubMed carries no license -> open_metadata
    assert {r["subset"] for r in rows} == {"open_metadata"}
    assert {r["license"] for r in rows} == {"unknown"}

    by_pmid = {r["pmid"]: r for r in rows}
    # (a) full record
    a = by_pmid["31452104"]
    assert a["doi"] == "10.1038/s41586-019-1552-1"
    assert a["year"] == 2019
    assert a["journal"] == "Nature"
    assert "acetylcholinesterase" in (a["text"] or "").lower()
    assert "Acetylcholinesterase" in (a["mesh"] or [])
    assert a["extract_status"] == "ok"
    # (c) structured-abstract-only record, year from <MedlineDate>
    c = by_pmid["30567891"]
    assert c["year"] == 2019
    assert "METHODS:" in (c["abstract"] or "")
    assert "RESULTS:" in (c["abstract"] or "")
    assert c["extract_status"] == "ok"


def test_pubmed_no_abstract_is_empty(tmp_path):
    extract_pubmed(FX, tmp_path)
    df = _read_shard(tmp_path / "staging" / "pubmed")
    statuses = set(df["extract_status"].to_list())
    assert "empty" in statuses  # the empty-title / no-abstract fixture record
    empt = next(r for r in df.to_dicts() if r["extract_status"] == "empty")
    assert empt["pmid"] == "28912345"
    assert empt["abstract"] is None
    assert empt["text"] is None


def test_pubmed_report_writes_nothing(tmp_path, capsys):
    rc = main(["--raw-dir", str(FX), "--processed-dir", str(tmp_path), "--report"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "extract_status" in out  # a field-shape table was printed
    # --report writes NO shards, NO markers, NO run manifest
    assert not (tmp_path / "staging").exists()
    assert not (tmp_path / "_ops").exists()


def test_pubmed_report_respects_max_files(tmp_path, capsys):
    """Fix 3 (whole-branch review): --report must apply --max-files, same as
    every other SP2 extractor's _run_report. Two raw-dir copies of the
    fixture so the cap is observable via the "files:" line of the printed
    field-shape table."""
    import shutil

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    shutil.copy(FX / "pubmed_sample.xml.gz", raw_dir / "pubmed_sample.xml.gz")
    shutil.copy(FX / "pubmed_sample.xml.gz", raw_dir / "pubmed_sample_2.xml.gz")

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

    # Pin directly to the "files=N  rows=M" line so this fails if a future
    # change caps rows instead of files, or differs for an incidental reason.
    assert "files=1  rows=" in capped_out
    assert "files=2  rows=" in uncapped_out
    assert not (tmp_path / "staging").exists()
    assert not (tmp_path / "_ops").exists()
