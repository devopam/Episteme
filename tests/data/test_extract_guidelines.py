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

import json
from pathlib import Path

import polars as pl

from episteme.data import article_schema
from episteme.data.guidelines.extract_guidelines import (
    discover,
    extract_guidelines,
    main,
    process_one,
)

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp2" / "guidelines"


def _shard_rows(staging_dir: Path) -> list[dict]:
    shards = sorted(staging_dir.glob("*.parquet")) + sorted(staging_dir.glob("*.jsonl"))
    out: list[dict] = []
    for s in shards:
        df = pl.read_parquet(s) if s.suffix == ".parquet" else pl.read_ndjson(s)
        out.extend(df.to_dicts())
    return out


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


def test_discover_stays_flat_ignores_nested_jsonl(tmp_path):
    """Judgment call (Task 15, mirrors Tasks 13/14): discover() stays flat --
    a *.jsonl in a subdirectory of raw_dir is NOT discovered. Confirmed
    against the real epfl-llm/guidelines HF repo's own file tree (queried
    directly for this task): siblings are exactly .gitattributes, LICENSE,
    README.md, open_guidelines.jsonl, sources.png -- all at repo root, no
    subdirectories, exactly one *.jsonl file. See discover()'s own docstring.
    """
    nested = tmp_path / "batch_a"
    nested.mkdir()
    (nested / "nested.jsonl").write_text(
        json.dumps({"id": "x", "clean_text": "hello world"}) + "\n", encoding="utf-8"
    )
    assert discover(tmp_path) == []


def test_same_basename_different_subdirs_distinct_markers_and_content(tmp_path):
    """Checkpoint-collision test, driven directly through process_one()
    (discover() by design never surfaces a nested path -- see the test
    above). Two files named dup.jsonl in different subdirectories under the
    same raw_dir, EACH carrying an explicit ``id`` column (the real
    epfl-llm/guidelines schema's shape, per the HF dataset_info API's
    declared ``id: string`` feature -- see the module docstring).

    Proves, per the brief's "not just marker existence" standard:
    - distinct, input_key-shaped source_file (batch_a__dup.jsonl /
      batch_b__dup.jsonl), not collapsed to the shared basename
    - two distinct, non-colliding success markers
    - two distinct shard files
    - each row's own token lands only in its own body_text, never the
      sibling same-basename file's (correct attachment, not just
      bookkeeping)
    - id-construction concern (explicit-id branch): id is content-derived
      (f"guidelines:{rec_id}"), so it stays correct AND distinct across the
      source_file collision -- source_file changing shape under this
      migration does not touch id at all on this branch.
    """
    raw_dir = tmp_path / "raw"
    processed_dir = tmp_path / "processed"
    (raw_dir / "batch_a").mkdir(parents=True)
    (raw_dir / "batch_b").mkdir(parents=True)

    file_a = raw_dir / "batch_a" / "dup.jsonl"
    file_a.write_text(json.dumps({"id": "docA", "clean_text": "TOKEN_A"}) + "\n", encoding="utf-8")
    file_b = raw_dir / "batch_b" / "dup.jsonl"
    file_b.write_text(json.dumps({"id": "docB", "clean_text": "TOKEN_B"}) + "\n", encoding="utf-8")

    res_a = process_one(file_a, raw_dir=raw_dir, processed_dir=processed_dir, force=False)
    res_b = process_one(file_b, raw_dir=raw_dir, processed_dir=processed_dir, force=False)

    assert res_a["ok"] and not res_a["skipped"]
    assert res_b["ok"] and not res_b["skipped"]
    assert res_a["source_file"] == "batch_a__dup.jsonl"
    assert res_b["source_file"] == "batch_b__dup.jsonl"

    # two distinct, non-colliding success markers
    success_dir = processed_dir / "_ops" / "guidelines" / "success"
    assert (success_dir / "batch_a__dup.jsonl.ok").exists()
    assert (success_dir / "batch_b__dup.jsonl.ok").exists()

    # two distinct shard files (not one overwriting the other)
    staging_dir = processed_dir / "staging" / "guidelines"
    shards = sorted(staging_dir.glob("*.parquet")) + sorted(staging_dir.glob("*.jsonl"))
    assert len(shards) == 2

    rows = _shard_rows(staging_dir)
    row_a = next(r for r in rows if r["source_file"] == "batch_a__dup.jsonl")
    row_b = next(r for r in rows if r["source_file"] == "batch_b__dup.jsonl")

    # correctly attached content -- not just distinct markers
    assert "TOKEN_A" in row_a["body_text"] and "TOKEN_B" not in row_a["body_text"]
    assert "TOKEN_B" in row_b["body_text"] and "TOKEN_A" not in row_b["body_text"]

    # id-construction concern: explicit-id branch is content-derived, stays
    # correct and distinct across the source_file collision
    assert row_a["id"] == "guidelines:docA"
    assert row_b["id"] == "guidelines:docB"
    assert row_a["id"] != row_b["id"]


def test_fallback_id_across_same_basename_subdirs_not_broken_by_migration(tmp_path):
    """Task 12's warning, applied to guidelines' fallback id branch
    (``f"{SOURCE}:{source_file}:{idx}"``, no id/doc_id/uid column present).

    This is the CRITICAL check the brief calls out: pre-migration
    (source_file = bare path.name), two same-basename files in DIFFERENT
    subdirectories would both compute id="guidelines:dup.jsonl:0" -- a real
    cross-document id collision (the bookshelf/apollo-branch-1 shape).
    Post-migration (source_file = input_key-shaped), the fallback ids are
    ALSO distinct, because they embed the now-disambiguated source_file.
    So for this synthetic, discover()-unreachable scenario, the migration
    does not break id -- if anything it removes a latent collision, though
    this is not reachable in production (discover() stays flat and the real
    corpus is exactly one root-level file, per discover()'s docstring).
    """
    raw_dir = tmp_path / "raw"
    processed_dir = tmp_path / "processed"
    (raw_dir / "batch_a").mkdir(parents=True)
    (raw_dir / "batch_b").mkdir(parents=True)

    file_a = raw_dir / "batch_a" / "dup.jsonl"
    file_a.write_text(json.dumps({"clean_text": "TOKEN_A"}) + "\n", encoding="utf-8")
    file_b = raw_dir / "batch_b" / "dup.jsonl"
    file_b.write_text(json.dumps({"clean_text": "TOKEN_B"}) + "\n", encoding="utf-8")

    res_a = process_one(file_a, raw_dir=raw_dir, processed_dir=processed_dir, force=False)
    res_b = process_one(file_b, raw_dir=raw_dir, processed_dir=processed_dir, force=False)
    assert res_a["ok"] and res_b["ok"]

    rows = _shard_rows(processed_dir / "staging" / "guidelines")
    row_a = next(r for r in rows if r["source_file"] == "batch_a__dup.jsonl")
    row_b = next(r for r in rows if r["source_file"] == "batch_b__dup.jsonl")

    assert row_a["id"] == "guidelines:batch_a__dup.jsonl:0"
    assert row_b["id"] == "guidelines:batch_b__dup.jsonl:0"
    # would have collided pre-migration (both "guidelines:dup.jsonl:0")
    assert row_a["id"] != row_b["id"]
