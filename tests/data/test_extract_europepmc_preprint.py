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
    discover,
    extract_europepmc_preprints,
    main,
    process_one,
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


def _make_preprint_xml(path: Path, *, token: str) -> None:
    """A minimal well-formed JATS ``<article>`` whose ``<body>`` embeds
    ``token``, long enough (>300 chars) to clear ``MIN_OK_TEXT_LEN`` (200) and
    land as ``extract_status="ok"``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    filler = f"Preprint body for {token}. " * 20
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<article article-type="preprint">'
        "<front><article-meta>"
        f'<article-id pub-id-type="doi">10.1101/{token}</article-id>'
        "<title-group><article-title>Fixture preprint</article-title></title-group>"
        "</article-meta></front>"
        f"<body><p>{filler}</p></body>"
        "</article>"
    )
    path.write_text(xml, encoding="utf-8")


def test_discover_stays_flat_ignores_nested_ppr_xml(tmp_path):
    """Task 13 judgment call, pinned as a regression guard for its sibling
    module: ``discover()`` deliberately keeps the pre-migration flat
    ``raw_dir.glob("PPR*.xml")`` scope (NOT
    ``checkpoint_markers.discover_input_files``, which also ``rglob``s) --
    ``download_europepmc_preprints.py``'s ``download_preprints()`` writes every
    fetched preprint with ``(raw_dir / f"{ppr_id}.xml").write_bytes(body)``, a
    bare join with no subdirectory component anywhere in the write path, so
    widening discovery here would be a real behavioural change with no known
    layout that needs it. A ``PPR*.xml`` placed in a subdirectory must NOT be
    discovered.
    """
    raw = tmp_path / "01_raw" / "europepmc_preprint"
    top_level = raw / "PPR1.xml"
    nested = raw / "batch_a" / "PPR2.xml"
    _make_preprint_xml(top_level, token="TOP")
    _make_preprint_xml(nested, token="NESTED")

    files = discover(raw)

    assert [f.resolve() for f in files] == [top_level.resolve()]


def test_same_basename_different_subdirs_distinct_markers_and_content(tmp_path):
    """Checkpoint-collision regression test (mirrors Task 13's
    ``europepmc_manuscript`` test), proving the identity migration to
    ``input_key`` even though ``discover()`` itself stays flat (see the
    judgment call recorded on ``discover()`` and in the commit message): two
    per-ID preprint files sharing an identical basename in different
    subdirectories under ``raw_dir``, driven directly through ``process_one``
    (the layer that DOES need to be collision-safe, since nothing about
    ``input_key``'s correctness should depend on whether ``discover()``
    happens to find a given path today -- and in fact never would for a
    same-basename nested pair, since the real per-ID downloader can only ever
    write one flat ``raw_dir/PPRid.xml`` per id).

    Proves distinct markers AND distinct, correctly-attached content -- not
    just marker existence: each file's own token must land on its OWN row,
    never the sibling same-basename file's.
    """
    raw = tmp_path / "01_raw" / "europepmc_preprint"
    file_a = raw / "batch_a" / "PPR1.xml"
    file_b = raw / "batch_b" / "PPR1.xml"
    _make_preprint_xml(file_a, token="TOKEN_A")
    _make_preprint_xml(file_b, token="TOKEN_B")

    processed = tmp_path / "02_processed"
    res_a = process_one(file_a, raw_dir=raw, processed_dir=processed, force=False)
    res_b = process_one(file_b, raw_dir=raw, processed_dir=processed, force=False)

    assert res_a["ok"] is True
    assert res_b["ok"] is True
    assert res_a["source_file"] != res_b["source_file"]
    assert res_a["source_file"] == "batch_a__PPR1.xml"
    assert res_b["source_file"] == "batch_b__PPR1.xml"

    # Distinct, non-colliding checkpoint markers.
    marks = sorted(
        p.name for p in (processed / "_ops" / "europepmc_preprint" / "success").glob("*.ok")
    )
    assert marks == ["batch_a__PPR1.xml.ok", "batch_b__PPR1.xml.ok"]

    # Two distinct shard files, not one clobbering the other.
    shard_dir = processed / "staging" / "europepmc_preprint"
    shards = list(shard_dir.glob("*.parquet")) or list(shard_dir.glob("*.jsonl"))
    assert len(shards) == 2

    rows = _all_rows(shard_dir)
    assert len(rows) == 2
    row_a = next(r for r in rows if r["source_file"] == "batch_a__PPR1.xml")
    row_b = next(r for r in rows if r["source_file"] == "batch_b__PPR1.xml")
    assert "TOKEN_A" in row_a["body_text"]
    assert "TOKEN_B" not in row_a["body_text"]
    assert "TOKEN_B" in row_b["body_text"]
    assert "TOKEN_A" not in row_b["body_text"]

    # id is doi-derived here (both fixtures carry a distinct doi), and this
    # migration never touches id construction in either branch (see the
    # module's parse_article: stem, not source_file, drives the fallback) --
    # pinned explicitly so a future change to id construction can't silently
    # start depending on the migrated source_file without a test noticing.
    assert row_a["id"] == "doi:10.1101/TOKEN_A"
    assert row_b["id"] == "doi:10.1101/TOKEN_B"
