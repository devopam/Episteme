import io
import json
import tarfile
from pathlib import Path

FX = Path(__file__).resolve().parents[1] / "fixtures" / "sp2" / "bookshelf"

# A minimal stand-in for the REAL NCBI Bookshelf BITS-book shape found live
# against a real download (see the Task 10 report) -- meaningfully different
# from the brief's <book> guess that FX above models:
#   * root is <book-part-wrapper>, not <book> -- no book-level <body>/<toc>/
#     <front> at all.
#   * <book-title-group><book-title>, not a bare <book-title>.
#   * exactly ONE <book-part>, with no `id` attribute -- its identifier lives
#     in <book-part-meta><book-part-id>, and its title is nested one level
#     deeper under <title-group><title>.
#   * licence lives in <book-meta><permissions><license><license-p>.
#   * the archive filename carries a report-id prefix before the NBK
#     accession (tr826967112990310_NBK599773.tar.gz on the real download).
_REAL_SHAPE_NXML = """<?xml version="1.0" encoding="UTF-8"?>
<book-part-wrapper id="toc" content-type="toc" dtd-version="2.0">
  <book-meta>
    <book-id book-id-type="pmcid">tr999</book-id>
    <book-title-group>
      <book-title>Zanubrutinib (Brukinsa) Fixture Recommendation</book-title>
    </book-title-group>
    <pub-date publication-format="electronic" date-type="pub">
      <month>09</month>
      <year>2023</year>
    </pub-date>
    <publisher><publisher-name>Fixture Health Technology Agency</publisher-name></publisher>
    <permissions>
      <license>
        <license-p>Copyright 2023, distributed under a Creative Commons
        Attribution-NonCommercial-NoDerivatives 4.0 International licence
        (CC BY-NC-ND).</license-p>
      </license>
    </permissions>
    <abstract>
      <title>Summary</title>
      <p>This plain-language summary exists only at book-meta level, distinct
      from the single book-part's technical body text below, so a test can
      assert the book row never absorbs part content by accident.</p>
    </abstract>
  </book-meta>
  <book-part book-part-type="section">
    <book-part-meta>
      <book-part-id book-part-id-type="pmcid">toc</book-part-id>
      <title-group>
        <title>Recommendation Detail</title>
      </title-group>
    </book-part-meta>
    <body>
      <p>The recommendation body text runs on at some length here so it
      comfortably clears the two-hundred character minimum the extract status
      rule checks for, covering fixture detail nobody will ever read closely
      but the extractor still has to hash, store and report on faithfully.</p>
    </body>
  </book-part>
</book-part-wrapper>
"""


def _write_real_shape_archive(tmp_path: Path) -> Path:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    tar_path = raw_dir / "tr999_NBK599773.tar.gz"
    data = _REAL_SHAPE_NXML.encode("utf-8")
    with tarfile.open(tar_path, "w:gz") as tar:
        info = tarfile.TarInfo(name="tr999_NBK599773/TOC.nxml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return raw_dir


def test_bookshelf_real_bits_shape(tmp_path):
    """Regression test for every adaptation the Task 10 report documents as
    found only against a live NCBI download (accession-from-filename,
    <book-title-group> / <title-group> nesting, <book-part-id> id fallback,
    the removed book-level <body> guard, the <abstract> book-text fallback,
    year-from-<pub-date>, and licence inheritance from book to part)."""
    from episteme.data.bookshelf.extract_bookshelf import extract_bookshelf

    raw_dir = _write_real_shape_archive(tmp_path / "src")
    out_dir = tmp_path / "out"
    res = extract_bookshelf(raw_dir, out_dir)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["rows"] == 2  # 1 book row + exactly 1 part row

    import polars as pl

    shard = sorted((out_dir / "staging" / "bookshelf").glob("*.*"))[0]
    rows = (
        pl.read_parquet(shard) if shard.suffix == ".parquet" else pl.read_ndjson(shard)
    ).to_dicts()

    book = [r for r in rows if r["container_id"] is None][0]
    parts = [r for r in rows if r["container_id"] is not None]

    # accession pulled from the report-id-prefixed filename, not the NXML body
    assert book["id"] == "bookshelf:NBK599773"
    assert book["source_file"] == "tr999_NBK599773.tar.gz"

    # title came through the nested <book-title-group><book-title>
    assert book["title"] == "Zanubrutinib (Brukinsa) Fixture Recommendation"

    # exactly one part row emitted despite no book-level <body>, its id from
    # <book-part-meta><book-part-id> (no `id` attribute on <book-part>), and
    # its own title from the nested <title-group><title>
    assert len(parts) == 1
    part = parts[0]
    assert part["id"] == "bookshelf:NBK599773:toc"
    assert part["container_id"] == "bookshelf:NBK599773"
    assert part["title"] == "Recommendation Detail"

    # book text is the <abstract> fallback (no <toc>/<front> in this shape)
    # and never absorbs the part's body content
    assert "plain-language summary" in (book["text"] or "")
    assert "recommendation body text" not in (book["text"] or "")
    assert "recommendation body text" in (part["text"] or "")

    # year parsed from <pub-date date-type="pub"><year>, inherited by the part
    assert book["year"] == 2023
    assert part["year"] == 2023

    # licence normalized from <permissions><license><license-p>, inherited by
    # the part (which carries no licence text of its own)
    assert book["license"] == "CC BY-NC-ND"
    assert book["subset"] == "text_mining"
    assert part["license"] == "CC BY-NC-ND"
    assert part["subset"] == "text_mining"


def test_bookshelf_book_plus_parts(tmp_path):
    from episteme.data.bookshelf.extract_bookshelf import extract_bookshelf

    res = extract_bookshelf(FX, tmp_path)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["failed"] == 0
    assert res["rows"] == 3

    import polars as pl

    shards = list((tmp_path / "staging" / "bookshelf").glob("*.*"))
    assert shards
    rows = (
        pl.read_parquet(shards[0]) if shards[0].suffix == ".parquet" else pl.read_ndjson(shards[0])
    ).to_dicts()

    book = [r for r in rows if r["id"] == "bookshelf:NBK1"][0]
    parts = [r for r in rows if r["container_id"] == "bookshelf:NBK1"]

    assert book["container_id"] is None
    assert book["book_meta"] is not None and "isbn" in book["book_meta"]
    assert "Chapter 1" not in (book["text"] or "")  # book text is TOC+front only
    assert {p["id"] for p in parts} == {"bookshelf:NBK1:p1", "bookshelf:NBK1:p2"}  # idx skipped
    assert all(p["container_id"] == "bookshelf:NBK1" for p in parts)

    # CRITICAL: book_meta must be a real JSON *string* that round-trips through
    # the parquet shard, not a raw dict whose Python repr would be invalid JSON.
    assert isinstance(book["book_meta"], str)
    parsed = json.loads(book["book_meta"])
    assert parsed["isbn"] == "978-0-000-00000-0"
    assert parsed["editors"] == ["Jane Doe"]
    assert parsed["publisher"] == "Fixture Press"
    assert parsed["edition"] == "1st"
    assert parsed["n_parts"] == 2  # kept parts only (idx excluded)

    # part rows carry no book_meta at all
    for p in parts:
        assert p["book_meta"] is None

    assert book["extract_status"] in ("ok", "partial")
    for p in parts:
        assert p["extract_status"] == "ok"


def test_bookshelf_report_writes_nothing(tmp_path, capsys):
    from episteme.data.bookshelf.extract_bookshelf import main

    rc = main(["--raw-dir", str(FX), "--processed-dir", str(tmp_path), "--report"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "bookshelf" in out
    assert "book_meta" in out

    # no shard / marker / manifest written
    assert not (tmp_path / "staging").exists()
    assert not (tmp_path / "_ops").exists()
