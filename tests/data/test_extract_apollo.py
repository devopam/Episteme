"""SP2 Task 6 — apollo extractor, reconciled to the extract_pubmed shape.

Fixture: tests/fixtures/sp2/apollo/apollo_sample.jsonl — 3 ApolloCorpus docs:
(a) English medical prose >= 300 chars, lang tag "en"; (b) Spanish medical prose
>= 300 chars, language tag "es" (proves multilingual survives with
APOLLO_MEDICAL_GATE=False); (c) short boilerplate < 100 chars -> extract_status
"dropped" via the length gate.

All `not pg` — no DB.
"""

from __future__ import annotations

import json
from pathlib import Path

from episteme.data import article_schema
from episteme.data.apollo.extract_apollo import discover_apollo_files, extract_apollo, main

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp2" / "apollo"

# Long enough, regardless of the per-record token prefixed onto it, to clear
# APOLLO_MIN_CHARS (200) so extract_status lands "ok", not "dropped" -- keeps
# the collision test's row-level assertions unambiguous.
_PAD = " padding text to exceed the two hundred character ok-text threshold" * 3


def _apollo_jsonl(path: Path, *, text: str) -> None:
    """Write a minimal one-record ApolloCorpus JSONL file (no ``id`` key --
    exercises the synthetic-id fallback path in ``_extract_id``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"text": text, "lang": "en"}) + "\n", encoding="utf-8")


def _read_shard(staging_dir: Path):
    import polars as pl

    shards = list(staging_dir.glob("*.parquet")) or list(staging_dir.glob("*.jsonl"))
    assert shards, f"no shard under {staging_dir}"
    s = shards[0]
    return pl.read_parquet(s) if s.suffix == ".parquet" else pl.read_ndjson(s)


def _read_all_rows(staging_dir: Path) -> list[dict]:
    import polars as pl

    rows: list[dict] = []
    for shard in sorted(staging_dir.glob("*.parquet")):
        rows.extend(pl.read_parquet(shard).to_dicts())
    for shard in sorted(staging_dir.glob("*.jsonl")):
        rows.extend(pl.read_ndjson(shard).to_dicts())
    return rows


def test_apollo_extract_rows(tmp_path):
    res = extract_apollo(FX, tmp_path)
    assert res["inputs"] >= 1
    assert res["failed"] == 0
    assert res["rows"] >= 1

    df = _read_shard(tmp_path / "staging" / "apollo")
    rows = df.to_dicts()
    assert {r["source"] for r in rows} == {"apollo"}
    assert all(r["extract_status"] in article_schema.EXTRACT_STATUSES for r in rows)
    # ApolloCorpus is not PubMed-indexed and carries no container.
    assert all(r["pmid"] is None for r in rows)
    assert all(r["pmcid"] is None for r in rows)
    assert all(r["container_id"] is None for r in rows)
    assert all(r["book_meta"] is None for r in rows)
    assert all(r["mesh"] is None for r in rows)

    # the English medical doc: language tag surfaced, text == body verbatim
    # (finalize_row did NOT rebuild text as title + "\n\n" + body)
    en = next(r for r in rows if "acetylcholinesterase" in (r["text"] or "").lower())
    assert en["language"] == "en"
    assert en["extract_status"] == "ok"
    assert en["text"] == en["body_text"]
    assert "\n\n" not in (en["text"] or "")  # no build_text title/body join
    assert en["title"] and en["title"] in en["text"]

    # the Spanish medical doc survives with the default gate off
    es = next(r for r in rows if r["language"] == "es")
    assert es["extract_status"] == "ok"
    assert "hipertension" in (es["text"] or "").lower()


def test_apollo_drops_boilerplate(tmp_path):
    extract_apollo(FX, tmp_path)
    df = _read_shard(tmp_path / "staging" / "apollo")
    rows = df.to_dicts()
    dropped = [r for r in rows if r["extract_status"] == "dropped"]
    assert len(dropped) == 1
    d = dropped[0]
    assert (d["extract_notes"] or "").startswith("drop_heuristic")
    assert "terms of use" in (d["text"] or "").lower()


def test_apollo_report_writes_nothing(tmp_path, capsys):
    rc = main(["--raw-dir", str(FX), "--processed-dir", str(tmp_path), "--report"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "extract_status" in out  # a field-shape table was printed
    # --report writes NO shards, NO markers, NO run manifest
    assert not (tmp_path / "staging").exists()
    assert not (tmp_path / "_ops").exists()


def test_discover_apollo_files_keeps_distinct_paths_same_basename(tmp_path):
    """Regression: the old ``list_input_files`` deduped by *basename* -- two
    files literally named ``apollo_dup.jsonl`` in different subdirectories
    would collapse to a single discovered file. ``discover_apollo_files`` must
    now discover both (dedup is by resolved full path).

    ``batch_a``/``batch_b`` here are synthetic, but the underlying shape is
    not purely hypothetical: ``download_apollo.sh`` builds no subpath of its
    own -- it delegates entirely to ``hf download --local-dir <raw_dir>``,
    which mirrors the ``FreedomIntelligence/ApolloCorpus`` HF repo's own
    directory tree byte-for-byte, so a same-basename collision across
    upstream subdirectories is layout-reachable, not something this test
    invents out of thin air.
    """
    raw = tmp_path / "01_raw" / "apollo"
    dir_a = raw / "batch_a"
    dir_b = raw / "batch_b"
    file_a = dir_a / "apollo_dup.jsonl"
    file_b = dir_b / "apollo_dup.jsonl"
    _apollo_jsonl(file_a, text=f"Document body for TOKEN_A.{_PAD}")
    _apollo_jsonl(file_b, text=f"Document body for TOKEN_B.{_PAD}")

    files = discover_apollo_files(raw)

    assert len(files) == 2
    assert {f.resolve() for f in files} == {file_a.resolve(), file_b.resolve()}


def test_same_basename_different_subdirs_both_processed_with_distinct_content(tmp_path):
    """Checkpoint-collision regression test (Task 12): two apollo
    ``*.jsonl`` files sharing an identical basename in different
    subdirectories under raw_dir. ``download_apollo.sh`` builds no subpath of
    its own -- it delegates entirely to ``hf download --local-dir <raw_dir>``,
    which mirrors whatever tree the upstream ``FreedomIntelligence/
    ApolloCorpus`` HF repo actually has, so this shape is layout-reachable in
    practice, not purely synthetic -- and nothing in the pre-migration
    ``list_input_files`` basename dedup enforced any particular layout
    regardless. The code path must be correct either way (the same standard
    Tasks 9/10/11 were held to for pmc/bookshelf/pubmed).

    Proves distinct markers AND distinct, correctly-attached content -- not
    just marker existence: each file's own token must land on its OWN row,
    never the sibling same-basename file's.
    """
    raw = tmp_path / "01_raw" / "apollo"
    dir_a = raw / "batch_a"
    dir_b = raw / "batch_b"
    file_a = dir_a / "apollo_dup.jsonl"
    file_b = dir_b / "apollo_dup.jsonl"
    _apollo_jsonl(file_a, text=f"Document body for TOKEN_A.{_PAD}")
    _apollo_jsonl(file_b, text=f"Document body for TOKEN_B.{_PAD}")

    processed = tmp_path / "02_processed"
    res = extract_apollo(raw, processed, workers=1)

    assert res["inputs"] == 2
    assert res["ok"] == 2
    assert res["failed"] == 0
    assert res["rows"] == 2

    # Distinct, non-colliding checkpoint markers -- each embeds its own
    # subdirectory, so the two do not collapse to one basename-keyed marker.
    marks = sorted(p.name for p in (processed / "_ops" / "apollo" / "success").glob("*.ok"))
    assert len(marks) == 2
    assert marks[0] != marks[1]
    assert set(marks) == {"batch_a__apollo_dup.jsonl.ok", "batch_b__apollo_dup.jsonl.ok"}

    # Two distinct shard files, not one clobbering the other.
    shard_dir = processed / "staging" / "apollo"
    shards = list(shard_dir.glob("*.parquet")) or list(shard_dir.glob("*.jsonl"))
    assert len(shards) == 2

    rows = _read_all_rows(shard_dir)
    assert len(rows) == 2
    by_source_file = {r["source_file"]: r for r in rows}

    # Distinct, correctly-attached source_file identity -- input_key, not the
    # shared basename both files carry.
    assert set(by_source_file) == {
        "batch_a__apollo_dup.jsonl",
        "batch_b__apollo_dup.jsonl",
    }

    row_a = by_source_file["batch_a__apollo_dup.jsonl"]
    row_b = by_source_file["batch_b__apollo_dup.jsonl"]

    # Correctly-attached content: each row's own token is present, and the
    # sibling same-basename file's token is absent -- proves no read/write
    # cross-attachment between the two same-basename files.
    assert "TOKEN_A" in (row_a["text"] or "")
    assert "TOKEN_B" not in (row_a["text"] or "")
    assert "TOKEN_B" in (row_b["text"] or "")
    assert "TOKEN_A" not in (row_b["text"] or "")

    # Distinct ids too, for THIS branch specifically: both fixture records
    # carry no id/uid/doc_id/uuid/sample_id key, so _extract_id falls back to
    # hashing source_file:idx:body[:200] (extract_apollo.py's _ID_KEYS miss
    # path). Pre-migration that hash was keyed off the shared, colliding
    # basename, so a same-basename collision was an id-collision risk too,
    # not just a source_file-attribution bug -- this migration fixes it for
    # the fallback-hash branch. It does NOT fix the other branch: two
    # records that DO carry the same explicit id key under different
    # upstream subdirectories still both become "apollo:{id}" regardless of
    # source_file, and postgres_loader's (d0) guard would still silently
    # discard one on load -- the bookshelf-shaped risk, pre-existing and out
    # of scope for this task (see the report's id-derivation section).
    assert row_a["id"] != row_b["id"]
