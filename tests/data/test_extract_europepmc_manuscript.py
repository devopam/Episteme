"""SP2 Task 8 — europepmc_manuscript extractor: EBI author-manuscript
``.tar.gz`` archives (one ``.txt`` + one ``.xml`` member per manuscript),
reconciled to the extract_pubmed / extract_europepmc_preprints shape.

Fixture: tests/fixtures/sp2/europepmc_manuscript/sample.tar.gz
  PMC0000001.txt — plain-text manuscript body (>300 chars) -> body_text only,
                   no title/abstract (the .txt format carries no structure).
  PMC0000002.xml — minimal well-formed JATS with <front> (pmcid/doi/title/
                   author/year) and a >300-char <body> -> parsed via the
                   shared episteme.data.jats.parse_jats_fields helper.

Both rows: source="europepmc_manuscript", is_manuscript=True,
subset="text_mining" (HARDCODED — EBI author manuscripts are provided for
text-mining under a specific licence, not an open one; a manuscript row must
never be eligible for the commercial corpus shard). container_id/book_meta
are None on every row (manuscripts are not book rows).

All `not pg` — no DB (the best-effort audit degrades to the JSONL/parquet
mirror; conftest.py's autouse guard blocks a real DB connection from a
non-pg test).
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from episteme.data import article_schema
from episteme.data.europepmc.manuscripts.extract_europepmc_manuscripts import (
    extract_europepmc_manuscripts,
    main,
)

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp2" / "europepmc_manuscript"


def _all_rows(staging_dir: Path):
    import polars as pl

    shards = list(staging_dir.glob("*.parquet")) or list(staging_dir.glob("*.jsonl"))
    out = []
    for s in shards:
        df = pl.read_parquet(s) if s.suffix == ".parquet" else pl.read_ndjson(s)
        out.extend(df.to_dicts())
    return out


def test_manuscript_extract_rows(tmp_path):
    res = extract_europepmc_manuscripts(FX, tmp_path)
    assert res["inputs"] == 1  # one archive
    assert res["failed"] == 0
    assert res["rows"] == 2  # one .txt member + one .xml member

    rows = _all_rows(tmp_path / "staging" / "europepmc_manuscript")
    assert len(rows) == 2
    assert {r["source"] for r in rows} == {"europepmc_manuscript"}
    assert all(r["is_manuscript"] is True for r in rows)
    assert all(r["subset"] == "text_mining" for r in rows)
    assert all(r["container_id"] is None for r in rows)
    assert all(r["book_meta"] is None for r in rows)
    assert all(r["extract_status"] in article_schema.EXTRACT_STATUSES for r in rows)

    # pmcid convention: keep the full "PMC" prefix (matches extract_pmc.py /
    # jats.parse_jats_fields, which both normalise to "PMC<digits>").
    txt_row = next(r for r in rows if r["source_record_id"] == "PMC0000001")
    xml_row = next(r for r in rows if r["source_record_id"] == "PMC0000002")

    assert txt_row["pmcid"] == "PMC0000001"
    assert txt_row["title"] is None
    assert txt_row["abstract"] is None
    assert txt_row["body_text"]
    assert txt_row["extract_status"] == "ok"

    assert xml_row["pmcid"] == "PMC0000002"
    assert xml_row["doi"] == "10.9999/fixture.0000002"
    assert xml_row["title"]
    assert xml_row["body_text"]
    assert xml_row["authors"] and "Farah Fixture" in xml_row["authors"]
    assert xml_row["extract_status"] == "ok"


def test_manuscript_never_commercial(tmp_path):
    extract_europepmc_manuscripts(FX, tmp_path)
    rows = _all_rows(tmp_path / "staging" / "europepmc_manuscript")
    assert len(rows) == 2
    for r in rows:
        assert r["subset"] == "text_mining"
        # Even if some future normalize_license/subset_from_license change
        # altered how "text mining / applicable copyright" classifies, the
        # extractor hardcodes subset="text_mining" for every manuscript row
        # (see extract_europepmc_manuscripts.py comment) -- so this can never
        # regress to "commercial" via that path either.
        assert r["subset"] != "commercial"

    # Cross-check against corpus_materializer.py's commercial-shard query:
    # it filters "WHERE a.subset = 'commercial' ..." (see
    # src/episteme/data/corpus_materializer.py), so any row with
    # subset="text_mining" is structurally excluded from that shard.
    materializer_src = (
        Path(__file__).resolve().parents[2] / "src" / "episteme" / "data" / "corpus_materializer.py"
    ).read_text(encoding="utf-8")
    assert "a.subset = 'commercial'" in materializer_src


def test_manuscript_report_writes_nothing(tmp_path, capsys):
    rc = main(["--raw-dir", str(FX), "--processed-dir", str(tmp_path), "--report"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "extract_status" in out  # a field-shape table was printed
    assert not (tmp_path / "staging").exists()
    assert not (tmp_path / "_ops").exists()


def test_manuscript_pmcid_ignores_directory_prefix(tmp_path):
    """Regression: real EBI archives nest members under an accession-range
    directory, e.g. ``PMC001xxxxxx/PMC1249490.xml`` -- the directory component
    itself starts with ``PMC`` + digits (``PMC001``) followed by literal ``x``
    placeholders, so a naive ``PMC\\d+`` search against the *full* member name
    matches the directory, not the manuscript's own id. Caught against a real
    9.3MB EBI archive (author_manuscript_xml.PMC001xxxxxx.baseline.2025-12-16
    .tar.gz) during this task's e2e check, where every one of 500 real rows
    came back with the same wrong pmcid="PMC001" before this fix.
    """
    archive = tmp_path / "raw" / "nested.tar.gz"
    archive.parent.mkdir(parents=True)
    xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b"<article><front><article-meta>"
        b"<title-group><article-title>Nested member</article-title></title-group>"
        b"</article-meta></front><body><p>" + b"x" * 300 + b"</p></body></article>"
    )
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name="PMC001xxxxxx/PMC1249490.xml")
        info.size = len(xml)
        tar.addfile(info, io.BytesIO(xml))

    out_dir = tmp_path / "out"
    res = extract_europepmc_manuscripts(tmp_path / "raw", out_dir)
    assert res["rows"] == 1

    rows = _all_rows(out_dir / "staging" / "europepmc_manuscript")
    assert rows[0]["pmcid"] == "PMC1249490"
    assert rows[0]["source_record_id"] == "PMC1249490"
