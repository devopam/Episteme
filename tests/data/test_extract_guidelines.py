"""SP2 Task 9 — guidelines extractor (epfl-llm/guidelines, pretraining split).

Fixture: tests/fixtures/sp2/guidelines/sample.jsonl -- 3 real-schema-shaped
clinical guideline docs (id, source, title, clean_text, raw_text, url,
overview -- the columns of the real ``epfl-llm/guidelines``
``open_guidelines.jsonl``, confirmed via the HF dataset-info API + a ranged
read of the live file at field-shape sign-off). A second file,
qa_split.jsonl, in the SAME fixture directory is QA-shaped
(question/answer/options/answer_idx) to prove the skip-heuristic: `discover()`
picks up both files, but only sample.jsonl contributes rows.

All `not pg` -- no DB.
"""

from __future__ import annotations

from pathlib import Path

from episteme.data import article_schema
from episteme.data.guidelines.extract_guidelines import extract_guidelines, main

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp2" / "guidelines"


def _read_shard(staging_dir: Path):
    import polars as pl

    shards = list(staging_dir.glob("*.parquet")) or list(staging_dir.glob("*.jsonl"))
    assert shards, f"no shard under {staging_dir}"
    s = shards[0]
    return pl.read_parquet(s) if s.suffix == ".parquet" else pl.read_ndjson(s)


def test_guidelines_extract_rows(tmp_path):
    res = extract_guidelines(FX, tmp_path)
    # 2 inputs discovered (sample.jsonl + qa_split.jsonl); only sample.jsonl
    # contributes rows -- the QA-shaped file is skipped, not errored.
    assert res["inputs"] == 2
    assert res["failed"] == 0
    assert res["rows"] == 3

    df = _read_shard(tmp_path / "staging" / "guidelines")
    rows = df.to_dicts()
    assert len(rows) == 3
    assert {r["source"] for r in rows} == {"guidelines"}
    assert all(r["extract_status"] in article_schema.EXTRACT_STATUSES for r in rows)
    for r in rows:
        assert r["subset"] == "other"
        assert r["license"] == "unknown"
        assert r["container_id"] is None
        assert r["book_meta"] is None
        assert r["pmid"] is None
        assert r["pmcid"] is None
        assert r["doi"] is None
        assert r["mesh"] is None

    # journal carries the issuing-body tag (real schema's `source` column)
    journals = {r["journal"] for r in rows}
    assert journals == {"nice", "cdc", "who"}

    # the NICE doc has a real title; the CDC/WHO docs' literal "None" string
    # title (a real quirk of the upstream dataset) normalises to NULL.
    nice_row = next(r for r in rows if r["journal"] == "nice")
    assert nice_row["title"] == "Type 2 diabetes in adults: management"
    cdc_row = next(r for r in rows if r["journal"] == "cdc")
    assert cdc_row["title"] is None


def test_guidelines_skips_qa_shaped_split(tmp_path):
    res = extract_guidelines(FX, tmp_path)
    df = _read_shard(tmp_path / "staging" / "guidelines")
    rows = df.to_dicts()
    # qa_split.jsonl contributed 0 rows: no row was misread out of its
    # question/answer/options columns, and it never crashed the extractor.
    assert not any(r["source_file"] == "qa_split.jsonl" for r in rows)
    assert res["inputs"] == 2
    assert res["rows"] == 3
    assert res["failed"] == 0


def test_guidelines_report_writes_nothing(tmp_path, capsys):
    rc = main(["--raw-dir", str(FX), "--processed-dir", str(tmp_path), "--report"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "extract_status" in out  # a field-shape table was printed
    # --report writes NO shards, NO markers, NO run manifest
    assert not (tmp_path / "staging").exists()
    assert not (tmp_path / "_ops").exists()
