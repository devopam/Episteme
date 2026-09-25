"""SP2 Task 5 — pubmed extractor, reconciled to the extract_pmc shape.

Fixture: tests/fixtures/sp2/pubmed/pubmed_sample.xml.gz — 3 <PubmedArticle>
records: (a) full narrative abstract + MeSH + DOI + year, (b) empty
<ArticleTitle/> and NO <Abstract> -> extract_status="empty", (c) structured
abstract only (Label=METHODS/RESULTS) with year via <MedlineDate>.

All `not pg` — no DB.
"""

from __future__ import annotations

import gzip
from pathlib import Path

from episteme.data import article_schema
from episteme.data.pubmed.extract_pubmed import discover_pubmed_files, extract_pubmed, main

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp2" / "pubmed"

# Long enough, regardless of the per-record token prefixed onto it, to clear
# article_schema.MIN_OK_TEXT_LEN (200) so extract_status lands "ok", not
# "partial" -- keeps the collision test's row-level assertions unambiguous.
_PAD = " padding text to exceed the two hundred character ok-text threshold" * 3


def _pubmed_xml_gz(path: Path, *, pmid: str, title: str, token: str) -> None:
    """Write a minimal one-record ``PubmedArticleSet`` gzip XML file.

    ``token`` is a unique marker embedded in both the title and abstract so a
    test can assert a row's content came from THIS file and not a sibling
    same-basename file elsewhere under raw_dir.
    """
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>{pmid}</PMID>
      <Article>
        <ArticleTitle>{title} [{token}]</ArticleTitle>
        <Abstract>
          <AbstractText>Abstract for {token}.{_PAD}</AbstractText>
        </Abstract>
        <Journal>
          <Title>Test Journal</Title>
          <JournalIssue><PubDate><Year>2023</Year></PubDate></JournalIssue>
        </Journal>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as f:
        f.write(xml.encode("utf-8"))


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


def _read_all_rows(staging_dir: Path) -> list[dict]:
    import polars as pl

    rows: list[dict] = []
    for shard in sorted(staging_dir.glob("*.parquet")):
        rows.extend(pl.read_parquet(shard).to_dicts())
    for shard in sorted(staging_dir.glob("*.jsonl")):
        rows.extend(pl.read_ndjson(shard).to_dicts())
    return rows


def test_discover_pubmed_files_keeps_distinct_paths_same_basename(tmp_path):
    """Regression: the old ``list_input_files`` deduped by *basename* -- two
    files literally named ``pubmed_dup.xml.gz`` in different subdirectories
    would collapse to a single discovered file. ``discover_pubmed_files`` must
    now discover both (dedup is by resolved full path).

    ``batch_a``/``batch_b`` here are synthetic, but the underlying shape is
    not purely hypothetical: the real downloader
    (``scripts/data/pubmed/download_pubmed.sh``) writes into exactly two
    sibling subdirectories, ``<raw_dir>/baseline/`` and
    ``<raw_dir>/updates/``, with no cross-mode pruning between separate
    ``baseline``/``updates`` invocations against the same ``raw_dir`` -- so a
    same-basename collision across those two real subdirectories is
    layout-reachable, not something this test invents out of thin air."""
    raw = tmp_path / "01_raw" / "pubmed"
    dir_a = raw / "batch_a"
    dir_b = raw / "batch_b"
    file_a = dir_a / "pubmed_dup.xml.gz"
    file_b = dir_b / "pubmed_dup.xml.gz"
    _pubmed_xml_gz(file_a, pmid="10000001", title="Article A", token="TOKEN_A")
    _pubmed_xml_gz(file_b, pmid="10000002", title="Article B", token="TOKEN_B")

    files = discover_pubmed_files(raw)

    assert len(files) == 2
    assert {f.resolve() for f in files} == {file_a.resolve(), file_b.resolve()}


def test_same_basename_different_subdirs_both_processed_with_distinct_content(tmp_path):
    """Checkpoint-collision regression test (Task 11): two ``pubmed*.xml.gz``
    files sharing an identical basename in different subdirectories under
    raw_dir. The real downloader (``scripts/data/pubmed/download_pubmed.sh``)
    writes into exactly two sibling subdirectories, ``baseline/`` and
    ``updates/``, with no cross-mode pruning between separate invocations --
    so this shape is layout-reachable in practice, not purely synthetic --
    and nothing in the pre-migration ``list_input_files`` basename dedup
    enforced any particular layout regardless. The code path must be correct
    either way (SP4.1 Task 3 discipline, and the same standard Tasks 9/10
    were held to for pmc/bookshelf).

    Proves distinct markers AND distinct, correctly-attached content -- not
    just marker existence (the exact gap Tasks 9/10's reviews flagged):
    each file's own PMID/title/abstract token must land on its OWN row, never
    the sibling same-basename file's.
    """
    raw = tmp_path / "01_raw" / "pubmed"
    dir_a = raw / "batch_a"
    dir_b = raw / "batch_b"
    file_a = dir_a / "pubmed_dup.xml.gz"
    file_b = dir_b / "pubmed_dup.xml.gz"
    _pubmed_xml_gz(file_a, pmid="10000001", title="Article A", token="TOKEN_A")
    _pubmed_xml_gz(file_b, pmid="10000002", title="Article B", token="TOKEN_B")

    processed = tmp_path / "02_processed"
    res = extract_pubmed(raw, processed, workers=1)

    assert res["inputs"] == 2
    assert res["ok"] == 2
    assert res["failed"] == 0
    assert res["rows"] == 2

    # Distinct, non-colliding checkpoint markers -- each embeds its own
    # subdirectory, so the two do not collapse to one basename-keyed marker.
    marks = sorted(p.name for p in (processed / "_ops" / "pubmed" / "success").glob("*.ok"))
    assert len(marks) == 2
    assert marks[0] != marks[1]
    assert any("batch_a" in m for m in marks)
    assert any("batch_b" in m for m in marks)
    assert set(marks) == {"batch_a__pubmed_dup.xml.gz.ok", "batch_b__pubmed_dup.xml.gz.ok"}

    # Two distinct shard files, not one clobbering the other.
    shard_dir = processed / "staging" / "pubmed"
    shards = list(shard_dir.glob("*.parquet")) or list(shard_dir.glob("*.jsonl"))
    assert len(shards) == 2

    rows = _read_all_rows(shard_dir)
    assert len(rows) == 2
    by_pmid = {r["pmid"]: r for r in rows}
    assert set(by_pmid) == {"10000001", "10000002"}

    row_a = by_pmid["10000001"]
    row_b = by_pmid["10000002"]

    # Distinct, correctly-attached source_file identity -- input_key, not the
    # shared basename both files carry.
    assert row_a["source_file"] == "batch_a__pubmed_dup.xml.gz"
    assert row_b["source_file"] == "batch_b__pubmed_dup.xml.gz"

    # Correctly-attached content: each row's own token is present, and the
    # SIBLING file's token is absent -- proves no read/write cross-attachment
    # between the two same-basename files, not just that two rows exist.
    assert "TOKEN_A" in row_a["title"]
    assert "TOKEN_A" in row_a["abstract"]
    assert "TOKEN_B" not in row_a["title"]
    assert "TOKEN_B" not in row_a["abstract"]

    assert "TOKEN_B" in row_b["title"]
    assert "TOKEN_B" in row_b["abstract"]
    assert "TOKEN_A" not in row_b["title"]
    assert "TOKEN_A" not in row_b["abstract"]


def test_discover_pubmed_files_baseline_still_sorts_first_under_real_nested_layout(tmp_path):
    """The baseline-before-updates ordering must survive the discovery-dedup
    migration: swapping the sort tiebreak from ``p.name`` to
    ``input_key(p, raw_dir)`` must not disturb the outer
    ``0 if "baseline" in str(p).lower() else 1`` priority. Exercised against
    the REAL nested layout (``<raw_dir>/baseline/`` + ``<raw_dir>/updates/``,
    per ``scripts/data/pubmed/download_pubmed.sh``), not the flat fixture
    copies ``test_pubmed_report_respects_max_files`` uses (which never
    exercise the tiebreak, since a flat file's ``input_key`` equals its
    ``.name``)."""
    raw = tmp_path / "01_raw" / "pubmed"
    update_file = raw / "updates" / "pubmed24n1220.xml.gz"
    baseline_file = raw / "baseline" / "pubmed24n0001.xml.gz"
    # Write updates' file first so a name-only sort would NOT happen to put
    # baseline first by coincidence.
    _pubmed_xml_gz(update_file, pmid="20000001", title="Update", token="UPD")
    _pubmed_xml_gz(baseline_file, pmid="20000002", title="Baseline", token="BASE")

    files = discover_pubmed_files(raw)

    assert len(files) == 2
    assert files[0].resolve() == baseline_file.resolve()
    assert files[1].resolve() == update_file.resolve()
