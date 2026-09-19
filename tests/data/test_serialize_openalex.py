"""SP4 Task 12 -- openalex structured serializer (bounded biomedical subset).

Fixture: tests/fixtures/sp4/openalex/sample.jsonl -- 3 REAL OpenAlex work
records, trimmed but field-shape-faithful, extracted verbatim from a real
partial S3 fetch of s3://openalex/data/jsonl/works/updated_date=2026-06-26/
part_0000.gz (byte-range 0-3000000, gunzip'd; the truncated final line was
dropped, leaving 1990 real, individually valid JSON records, of which these
3 were selected):

  1. ``openalex:W7165474278`` -- "Awareness and Fears Regarding Immunization
     Among Guardians of Children Visiting HBS General Hospital Islamabad" --
     BIOMEDICAL via the real level-0 "Medicine" concept (id C71924100), HAS a
     real ``abstract_inverted_index`` (229 words, "and" recurring at 12 real
     distinct positions -- proves the reconstruction helper's sort-by-
     position approach, not a naive alphabetical-by-word one).

  2. ``openalex:W7165526654`` -- "Himalayan Herbal AI: ML Pipeline for
     Antimicrobial Drug Discovery from Himalayan Medicinal Plant Compounds"
     -- BIOMEDICAL via the real level-0 "Biology" concept (id C86803240,
     the OTHER accepted branch), also has a real abstract.

  3. ``openalex:W6935257181`` -- "LHD NB4arm #124579.242" (a nuclear-fusion
     hardware dataset record) -- NOT biomedical (real level-0 concepts:
     Materials science / Engineering / Physics only) -- MUST be rejected by
     the filter (counted, not written as a row), despite itself carrying a
     real, non-empty abstract_inverted_index -- proves the filter runs
     independently of text-quality signals.

All `not pg` -- no DB.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from episteme.data import article_schema
from episteme.data.checkpoint_markers import input_key
from episteme.data.openalex.serialize_openalex import (
    discover_openalex_files,
    is_biomedical,
    iter_rows_from_file,
    main,
    reconstruct_abstract,
    serialize_openalex,
)

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp4" / "openalex"


def _read_shard(staging_dir: Path) -> pl.DataFrame:
    shards = list(staging_dir.glob("*.parquet")) or list(staging_dir.glob("*.jsonl"))
    assert shards, f"no shard under {staging_dir}"
    s = shards[0]
    return pl.read_parquet(s) if s.suffix == ".parquet" else pl.read_ndjson(s)


# --------------------------------------------------------------------------- #
# reconstruct_abstract -- pure helper, unit-tested in isolation
# --------------------------------------------------------------------------- #


def test_reconstruct_abstract_brief_example():
    """The brief's own worked example."""
    aii = {"The": [0], "study": [1], "shows": [2]}
    assert reconstruct_abstract(aii) == "The study shows"


def test_reconstruct_abstract_repeated_word_multiple_positions():
    """A word recurring at multiple positions must NOT collapse or dedupe --
    every occurrence must land back at its own real position. Also proves
    the flatten-then-sort-by-POSITION approach does not degrade to a naive
    ``sorted(dict.items())`` (which would sort ALPHABETICALLY BY WORD --
    "The" < "and" < "cat" < "on" < "sat" -- producing the wrong sentence)."""
    aii = {"the": [0, 3], "cat": [1], "sat": [2], "on": [4], "mat": [5]}
    # Correct (position-sorted): "the cat sat the on mat"
    # Wrong (word-sorted / naive dict-item sort): "cat mat on sat the"
    assert reconstruct_abstract(aii) == "the cat sat the on mat"


def test_reconstruct_abstract_empty_or_none():
    assert reconstruct_abstract(None) is None
    assert reconstruct_abstract({}) is None


def test_reconstruct_abstract_single_word_multiple_positions():
    aii = {"hello": [0, 1, 2]}
    assert reconstruct_abstract(aii) == "hello hello hello"


# --------------------------------------------------------------------------- #
# is_biomedical -- pure helper, unit-tested in isolation
# --------------------------------------------------------------------------- #


def test_is_biomedical_medicine_level0():
    rec = {
        "concepts": [
            {"id": "https://openalex.org/C71924100", "display_name": "Medicine", "level": 0}
        ]
    }
    assert is_biomedical(rec) is True


def test_is_biomedical_biology_level0():
    rec = {
        "concepts": [
            {"id": "https://openalex.org/C86803240", "display_name": "Biology", "level": 0}
        ]
    }
    assert is_biomedical(rec) is True


def test_is_biomedical_rejects_non_biomedical_level0():
    rec = {
        "concepts": [
            {
                "id": "https://openalex.org/C192562407",
                "display_name": "Materials science",
                "level": 0,
            },
            {"id": "https://openalex.org/C127413603", "display_name": "Engineering", "level": 0},
        ]
    }
    assert is_biomedical(rec) is False


def test_is_biomedical_ignores_non_level0_medicine_lookalike():
    """A specific (non-top-level) concept whose NAME merely resembles
    "Medicine"/"Biology" at a deeper level must not false-positive -- only a
    real LEVEL-0 id match counts."""
    rec = {
        "concepts": [
            {
                "id": "https://openalex.org/C99999999",
                "display_name": "Traditional medicine",
                "level": 1,
            },
        ]
    }
    assert is_biomedical(rec) is False


def test_is_biomedical_empty_concepts():
    assert is_biomedical({"concepts": []}) is False
    assert is_biomedical({}) is False


# --------------------------------------------------------------------------- #
# End-to-end: real (trimmed) fixture rows
# --------------------------------------------------------------------------- #


def test_openalex_serialize_rows(tmp_path):
    res = serialize_openalex(FX, tmp_path)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["failed"] == 0
    # 3 real records in the fixture, 1 rejected (non-biomedical) -> 2 rows.
    assert res["rows"] == 2
    assert res["n_records_seen"] == 3
    assert res["n_accepted_biomedical"] == 2
    assert res["n_rejected_non_biomedical"] == 1

    df = _read_shard(tmp_path / "staging" / "openalex")
    rows = df.to_dicts()

    assert {row["source"] for row in rows} == {"openalex"}
    assert all(row["id"].startswith("openalex:") for row in rows)
    assert len({row["id"] for row in rows}) == 2
    assert {row["id"] for row in rows} == {
        "openalex:W7165474278",
        "openalex:W7165526654",
    }
    # The non-biomedical record must NOT appear at all.
    assert "openalex:W6935257181" not in {row["id"] for row in rows}

    assert all(row["license"] == "CC0" for row in rows)
    assert all(row["subset"] == "commercial" for row in rows)
    assert all(row["license_raw"] is not None for row in rows)

    assert all(row["container_id"] is None for row in rows)
    assert all(row["book_meta"] is None for row in rows)
    assert all(row["pmid"] is None and row["pmcid"] is None for row in rows)
    assert all(row["extract_status"] in article_schema.EXTRACT_STATUSES for row in rows)

    by_id = {row["id"]: row for row in rows}

    immun = by_id["openalex:W7165474278"]
    assert immun["doi"] == "10.69884/hmdj.5.2.1501"
    assert immun["title"] == (
        "Awareness and Fears Regarding Immunization Among Guardians of "
        "Children Visiting HBS General Hospital Islamabad"
    )
    assert immun["year"] == 2026
    assert immun["authors"] == [
        "Ameena Saba",
        "Mahwish Rabia",
        "Tooba Riaz",
        "Samia Mehmood",
        "Rabia Iqbal",
    ]
    assert immun["journal"] == "HITEC medical and dental journal."
    # Reconstructed abstract text, in real word order (starts with the real
    # "Objective: To assess awareness ..." opening, not an inverted dict).
    assert immun["abstract"].startswith("Objective: To assess awareness and fears")
    assert immun["abstract"] in immun["text"]
    assert immun["extract_status"] == "ok"

    herbal = by_id["openalex:W7165526654"]
    assert herbal["doi"] == "10.5281/zenodo.20792814"
    assert herbal["journal"] == "Zenodo (CERN European Organization for Nuclear Research)"
    assert herbal["abstract"].startswith(
        "A machine learning pipeline for antimicrobial drug discovery"
    )
    assert herbal["extract_status"] == "ok"


def test_openalex_report_writes_nothing(tmp_path, capsys):
    rc = main(["--raw-dir", str(FX), "--processed-dir", str(tmp_path), "--report"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "extract_status" in out
    assert "bounded biomedical filter" in out
    assert "records_seen=3" in out
    assert "accepted=2" in out
    assert "rejected_non_biomedical=1" in out
    assert not (tmp_path / "staging").exists()
    assert not (tmp_path / "_ops").exists()


def test_openalex_serialize_is_idempotent_without_force(tmp_path):
    res1 = serialize_openalex(FX, tmp_path)
    assert res1["ok"] == 1
    res2 = serialize_openalex(FX, tmp_path)
    assert res2["inputs"] == 1
    assert res2["ok"] == 0  # skipped, not re-ok'd
    assert res2["failed"] == 0

    res3 = serialize_openalex(FX, tmp_path, force=True)
    assert res3["ok"] == 1


def test_openalex_gzip_shard_is_supported(tmp_path):
    """Real OpenAlex S3 object keys land as bare ``part_NNNN.gz`` (gzip-
    compressed, no ``.jsonl`` in the name) -- confirm a gzip-compressed shard
    parses identically to the bare fixture."""
    import gzip
    import shutil

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    src = FX / "sample.jsonl"
    dst = raw_dir / "part_0000.gz"
    with src.open("rb") as fsrc, gzip.open(dst, "wb") as fdst:
        shutil.copyfileobj(fsrc, fdst)

    processed_dir = tmp_path / "processed"
    res = serialize_openalex(raw_dir, processed_dir)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["rows"] == 2

    df = _read_shard(processed_dir / "staging" / "openalex")
    rows = df.to_dicts()
    assert {row["id"] for row in rows} == {
        "openalex:W7165474278",
        "openalex:W7165526654",
    }


def test_openalex_report_respects_max_files(tmp_path, capsys):
    import shutil

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    shutil.copy(FX / "sample.jsonl", raw_dir / "sample.jsonl")
    shutil.copy(FX / "sample.jsonl", raw_dir / "sample_2.jsonl")

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

    assert "files=1  rows=2" in capped_out
    assert "files=2  rows=4" in uncapped_out


def test_openalex_malformed_json_line_is_skipped_not_fatal(tmp_path):
    from collections import Counter

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    good = (FX / "sample.jsonl").read_text(encoding="utf-8")
    bad_path = raw_dir / "with_bad_line.jsonl"
    bad_path.write_text(good + "{not valid json\n", encoding="utf-8")

    counter: Counter = Counter()
    rows = list(iter_rows_from_file(bad_path, counter))
    assert len(rows) == 2  # the 2 real biomedical records still parse fine
    assert counter["n_parse_errors"] == 1
    assert counter["n_records"] == 3
    assert counter["n_rejected_non_biomedical"] == 1


def test_openalex_no_files_under_raw_dir(tmp_path):
    empty = tmp_path / "empty_raw"
    empty.mkdir()
    res = serialize_openalex(empty, tmp_path / "processed")
    assert res["inputs"] == 0
    assert res["rows"] == 0


def test_openalex_discovers_same_basename_across_partitions(tmp_path):
    """Real OpenAlex S3 keys land as bare ``part_0000.gz`` REPEATED
    identically across every real ``updated_date=YYYY-MM-DD/`` partition
    directory (confirmed against the real bucket listing). A basename-only
    dedup (e.g. ``checkpoint_markers.list_input_files``' own dedup, used by
    every OTHER SP4 structured serializer's discovery) would silently DROP
    every same-named file but the first one found -- this must NOT happen
    here: both real partitions' files must be discovered, processed
    independently, and land as two distinct staging shards / success
    markers (not one overwriting the other)."""
    import gzip

    raw_dir = tmp_path / "raw"
    p1 = raw_dir / "updated_date=2026-06-25"
    p2 = raw_dir / "updated_date=2026-06-26"
    p1.mkdir(parents=True)
    p2.mkdir(parents=True)
    src = FX / "sample.jsonl"
    with src.open("rb") as fsrc:
        data = fsrc.read()
    with gzip.open(p1 / "part_0000.gz", "wb") as f:
        f.write(data)
    with gzip.open(p2 / "part_0000.gz", "wb") as f:
        f.write(data)

    files = discover_openalex_files(raw_dir)
    assert len(files) == 2, "same-basename files across partitions must NOT collide at discovery"
    qualified = {input_key(f, raw_dir) for f in files}
    assert qualified == {
        "updated_date=2026-06-25__part_0000.gz",
        "updated_date=2026-06-26__part_0000.gz",
    }

    processed_dir = tmp_path / "processed"
    res = serialize_openalex(raw_dir, processed_dir)
    assert res["inputs"] == 2
    assert res["ok"] == 2
    assert res["failed"] == 0
    # 2 accepted rows per shard (see fixture docstring) x 2 shards = 4 rows,
    # NOT 2 (which would indicate the second partition's shard silently
    # overwrote the first's, or was never discovered at all).
    assert res["rows"] == 4

    shards = list((processed_dir / "staging" / "openalex").glob("*.parquet"))
    assert len(shards) == 2, "one staging shard per partition, no overwrite"
    markers = list((processed_dir / "_ops" / "openalex" / "success").glob("*.ok"))
    assert len(markers) == 2, "one success marker per partition, no collision"


def test_input_key_flat_layout_keeps_bare_name():
    """A flat/manually-supplied raw_dir (no updated_date=.../ nesting --
    this module's own fixture/test layout) must keep the BARE filename
    unchanged, matching every other SP4 structured serializer's
    convention -- nesting only qualifies the key."""
    assert input_key(FX / "sample.jsonl", FX) == "sample.jsonl"
