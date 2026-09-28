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
    discover,
    extract_europepmc_manuscripts,
    main,
    process_one,
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


def test_manuscript_id_disambiguates_txt_vs_xml_same_pmcid(tmp_path):
    """Regression (code review on 9e0f8bd): EBI ships this source as two
    parallel archive families over the *same* accession ranges
    (``author_manuscript_txt.*`` / ``author_manuscript_xml.*`` -- see
    ``download_europepmc_manuscript.sh``'s ``FMT`` argument). Before this
    fix, both formats' rows for one manuscript shared
    ``id = f"{SOURCE}:{pmcid}"`` with no format suffix -- since
    ``episteme.articles`` has no PK/unique constraint on ``id`` and
    ``postgres_loader``'s idempotency keys on ``source_file`` (not ``id``),
    extracting + loading both format archives for one manuscript would
    silently produce two rows sharing one ``id``. ``source_record_id`` must
    stay the bare ``pmcid`` -- only ``id`` gets the format suffix.
    """
    archive = tmp_path / "raw" / "same_pmcid_both_formats.tar.gz"
    archive.parent.mkdir(parents=True)
    txt_body = ("Same-manuscript plain-text member. " * 20).encode("utf-8")
    xml_body = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b"<article><front><article-meta>"
        b"<title-group><article-title>Same manuscript, xml format</article-title></title-group>"
        b"</article-meta></front><body><p>" + b"x" * 300 + b"</p></body></article>"
    )
    with tarfile.open(archive, "w:gz") as tar:
        for name, data in (
            ("PMC9999999.txt", txt_body),
            ("PMC9999999.xml", xml_body),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

    out_dir = tmp_path / "out"
    res = extract_europepmc_manuscripts(tmp_path / "raw", out_dir)
    assert res["rows"] == 2

    rows = _all_rows(out_dir / "staging" / "europepmc_manuscript")
    assert len(rows) == 2
    assert all(r["pmcid"] == "PMC9999999" for r in rows)
    assert all(r["source_record_id"] == "PMC9999999" for r in rows)

    ids = {r["id"] for r in rows}
    assert len(ids) == 2, f"expected two distinct ids, got a collision: {ids}"
    assert ids == {"europepmc_manuscript:PMC9999999:txt", "europepmc_manuscript:PMC9999999:xml"}


def _make_archive(path: Path, *, token: str) -> None:
    """A minimal one-``.txt``-member archive whose body embeds ``token``, long
    enough to clear the ``ok`` text-length gate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"Manuscript body for {token}. ".encode() * 20
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(name="PMC0000042.txt")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))


def test_discover_stays_flat_ignores_nested_tar_gz(tmp_path):
    """Task 13 judgment call, pinned as a regression guard: ``discover()``
    deliberately keeps the pre-migration flat ``raw_dir.glob("*.tar.gz")``
    scope (NOT ``checkpoint_markers.discover_input_files``, which also
    ``rglob``s) -- ``download_europepmc_manuscript.sh`` states outright
    ("Flat EBI directory") that the real upstream layout has no
    subdirectories, so widening discovery here would be a real behavioural
    change with no known layout that needs it (unlike apollo's HF-mirrored,
    layout-uncontrolled downloader). A ``.tar.gz`` placed in a subdirectory
    must NOT be discovered.
    """
    raw = tmp_path / "01_raw" / "europepmc_manuscript"
    top_level = raw / "top_level.tar.gz"
    nested = raw / "batch_a" / "nested.tar.gz"
    _make_archive(top_level, token="TOP")
    _make_archive(nested, token="NESTED")

    files = discover(raw)

    assert [f.resolve() for f in files] == [top_level.resolve()]


def test_same_basename_different_subdirs_distinct_markers_and_content(tmp_path):
    """Checkpoint-collision regression test (Task 13), proving the identity
    migration to ``input_key`` even though ``discover()`` itself stays flat
    (see the judgment call recorded on ``discover()`` and in the commit
    message): two archives sharing an identical basename in different
    subdirectories under ``raw_dir``, driven directly through ``process_one``
    (the layer that DOES need to be collision-safe, since nothing about
    ``input_key``'s correctness should depend on whether ``discover()``
    happens to find a given path today).

    Proves distinct markers AND distinct, correctly-attached content -- not
    just marker existence: each archive's own token must land on its OWN
    row, never the sibling same-basename archive's.
    """
    raw = tmp_path / "01_raw" / "europepmc_manuscript"
    file_a = raw / "batch_a" / "dup.tar.gz"
    file_b = raw / "batch_b" / "dup.tar.gz"
    _make_archive(file_a, token="TOKEN_A")
    _make_archive(file_b, token="TOKEN_B")

    processed = tmp_path / "02_processed"
    res_a = process_one(file_a, raw_dir=raw, processed_dir=processed, force=False)
    res_b = process_one(file_b, raw_dir=raw, processed_dir=processed, force=False)

    assert res_a["ok"] is True
    assert res_b["ok"] is True
    assert res_a["source_file"] != res_b["source_file"]
    assert res_a["source_file"] == "batch_a__dup.tar.gz"
    assert res_b["source_file"] == "batch_b__dup.tar.gz"

    # Distinct, non-colliding checkpoint markers.
    marks = sorted(
        p.name for p in (processed / "_ops" / "europepmc_manuscript" / "success").glob("*.ok")
    )
    assert marks == ["batch_a__dup.tar.gz.ok", "batch_b__dup.tar.gz.ok"]

    # Two distinct shard files, not one clobbering the other.
    shard_dir = processed / "staging" / "europepmc_manuscript"
    shards = list(shard_dir.glob("*.parquet")) or list(shard_dir.glob("*.jsonl"))
    assert len(shards) == 2

    rows = _all_rows(shard_dir)
    assert len(rows) == 2
    row_a = next(r for r in rows if r["source_file"] == "batch_a__dup.tar.gz")
    row_b = next(r for r in rows if r["source_file"] == "batch_b__dup.tar.gz")
    assert "TOKEN_A" in row_a["body_text"]
    assert "TOKEN_B" not in row_a["body_text"]
    assert "TOKEN_B" in row_b["body_text"]
    assert "TOKEN_A" not in row_b["body_text"]
