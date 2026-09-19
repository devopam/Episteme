"""SP4 Task 6 -- uniprot structured serializer.

Fixture: tests/fixtures/sp4/uniprot/sample.fasta -- 3 real Swiss-Prot FASTA
entries extracted verbatim (header + sequence) from a real
`uniprot_sprot.fasta.gz` download:
  * P68871 HBB_HUMAN  -- full header shape (OS=/OX=/GN=/PE=/SV= all present).
  * Q9VJ04 10702_DROME -- full header shape, second well-populated example.
  * A9CBA2 105R_ADES1 -- deliberately missing GN= (a real Swiss-Prot entry
    that genuinely carries no gene-name field) -- exercises the
    "Unknown gene" graceful-degradation default.

All `not pg` -- no DB.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from episteme.data import article_schema
from episteme.data.uniprot.serialize_uniprot import iter_rows_from_file, main, serialize_uniprot

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp4" / "uniprot"


def _read_shard(staging_dir: Path) -> pl.DataFrame:
    shards = list(staging_dir.glob("*.parquet")) or list(staging_dir.glob("*.jsonl"))
    assert shards, f"no shard under {staging_dir}"
    s = shards[0]
    return pl.read_parquet(s) if s.suffix == ".parquet" else pl.read_ndjson(s)


def test_uniprot_serialize_rows(tmp_path):
    res = serialize_uniprot(FX, tmp_path)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["failed"] == 0
    assert res["rows"] == 3

    shards = list((tmp_path / "staging" / "uniprot").glob("*.*"))
    assert shards
    df = _read_shard(tmp_path / "staging" / "uniprot")
    rows = df.to_dicts()

    assert {row["source"] for row in rows} == {"uniprot"}
    assert all(row["id"].startswith("uniprot:") for row in rows)
    assert len({row["id"] for row in rows}) == 3
    assert {row["id"] for row in rows} == {
        "uniprot:P68871",
        "uniprot:Q9VJ04",
        "uniprot:A9CBA2",
    }

    # subset is HARDCODED text_mining -- NOT whatever subset_from_license(license)
    # would otherwise compute for the real UniProt CC BY 4.0 licence string.
    assert all(row["subset"] == "text_mining" for row in rows)
    # normalize_license() is still called for real -- only subset is overridden.
    # Real UniProt LICENSE file text ("Creative Commons Attribution 4.0
    # International (CC BY 4.0) License") normalizes to license="CC BY" (NOT
    # "CC BY-ND" -- confirmed against the actual LICENSE file at
    # ftp.uniprot.org/.../knowledgebase/complete/LICENSE at implementation time,
    # see task-6-report.md).
    assert all(row["license"] == "CC BY" for row in rows)
    assert all(row["license_url"] == "https://creativecommons.org/licenses/by/4.0/" for row in rows)

    assert all(row["container_id"] is None for row in rows)
    assert all(row["book_meta"] is None for row in rows)
    # UniProt FASTA records carry no bibliographic shape.
    assert all(row["title"] is None for row in rows)
    assert all(row["journal"] is None for row in rows)
    assert all(row["year"] is None for row in rows)
    assert all(row["authors"] is None for row in rows)
    assert all(row["pmid"] is None and row["pmcid"] is None and row["doi"] is None for row in rows)
    assert all(row["extract_status"] in article_schema.EXTRACT_STATUSES for row in rows)
    # The sequence alone comfortably clears MIN_OK_TEXT_LEN (200 chars) for
    # every fixture entry -- see task-6-report.md's length sanity-check.
    assert all(row["extract_status"] == "ok" for row in rows)

    by_id = {row["id"]: row for row in rows}

    hbb = by_id["uniprot:P68871"]
    assert "HBB_HUMAN" in hbb["text"]
    assert "Hemoglobin subunit beta" in hbb["text"]
    assert "Homo sapiens" in hbb["text"]
    assert "encoded by gene HBB" in hbb["text"]
    assert "MVHLTPEEKSAVTALWGKVNVDEVGGEALGRLLVVYPWTQRFFESFGDLSTPDAVMGNPK" in hbb["text"]

    # A9CBA2's real header has no GN= -- must degrade to "Unknown gene", not
    # raise or silently drop the record.
    orphan_gene = by_id["uniprot:A9CBA2"]
    assert "encoded by gene Unknown gene" in orphan_gene["text"]
    assert "Snake adenovirus serotype 1" in orphan_gene["text"]
    assert orphan_gene["extract_status"] == "ok"


def test_uniprot_malformed_header_fails_whole_file(tmp_path):
    """A genuinely malformed header (missing the `|` pipe separators entirely)
    must NOT silently emit a fake 'uniprot_unknown' row (the pre-restructure
    prototype's behaviour) -- it must fail loudly. Per-file granularity
    (matching extract_pubmed.py's process_one): one malformed record fails
    the WHOLE file, mirrored via mark_failed -- see task-6-report.md for the
    reasoning.
    """
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    bad = raw_dir / "bad.fasta"
    bad.write_text(
        ">this header has no pipes at all\n"
        "MVHLTPEEKSAVTALWGKVNVDEVGGEALGRLLVVYPWTQRFFESFGDLSTPDAVMGNPK\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        list(iter_rows_from_file(bad))

    processed_dir = tmp_path / "processed"
    res = serialize_uniprot(raw_dir, processed_dir)
    assert res["inputs"] == 1
    assert res["ok"] == 0
    assert res["failed"] == 1

    failed_marker = processed_dir / "_ops" / "uniprot" / "failed" / "bad.fasta.json"
    assert failed_marker.is_file()


def test_uniprot_serialize_is_idempotent_without_force(tmp_path):
    res1 = serialize_uniprot(FX, tmp_path)
    assert res1["ok"] == 1
    # Second run without --force must skip (success marker present).
    res2 = serialize_uniprot(FX, tmp_path)
    assert res2["inputs"] == 1
    assert res2["ok"] == 0  # skipped, not re-ok'd
    assert res2["failed"] == 0

    res3 = serialize_uniprot(FX, tmp_path, force=True)
    assert res3["ok"] == 1


def test_uniprot_gzip_input_is_supported(tmp_path):
    """The real download_uniprot.sh artifact is `uniprot_sprot.fasta.gz`
    (gzip, not a bare .fasta) -- confirm gzip-compressed input parses
    identically to the bare fixture."""
    import gzip
    import shutil

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    gz_path = raw_dir / "uniprot_sprot.fasta.gz"
    with (FX / "sample.fasta").open("rb") as src, gzip.open(gz_path, "wb") as dst:
        shutil.copyfileobj(src, dst)

    processed_dir = tmp_path / "processed"
    res = serialize_uniprot(raw_dir, processed_dir)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["rows"] == 3

    df = _read_shard(processed_dir / "staging" / "uniprot")
    rows = df.to_dicts()
    assert {row["id"] for row in rows} == {
        "uniprot:P68871",
        "uniprot:Q9VJ04",
        "uniprot:A9CBA2",
    }


def test_uniprot_report_writes_nothing(tmp_path, capsys):
    rc = main(["--raw-dir", str(FX), "--processed-dir", str(tmp_path), "--report"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "extract_status" in out
    assert not (tmp_path / "staging").exists()
    assert not (tmp_path / "_ops").exists()


def test_uniprot_report_respects_max_files(tmp_path, capsys):
    import shutil

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    shutil.copy(FX / "sample.fasta", raw_dir / "sample.fasta")
    shutil.copy(FX / "sample.fasta", raw_dir / "sample_2.fasta")

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
