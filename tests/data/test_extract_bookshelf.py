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


# --------------------------------------------------------------------------- #
# Checkpoint-collision regression test (Task 10)
#
# The REAL bookshelf download layout (scripts/data/bookshelf/download_bookshelf.sh)
# writes every package to ``<raw_dir>/packages/<rel>`` where ``<rel>`` is the
# path exactly as listed in NCBI LitArch's file_list.txt -- a hashed tree with
# no usable directory listing (see the script's own header comment). Nothing
# stops two hash buckets from each holding a package with the identical
# basename (e.g. a re-released/updated package landing in a new bucket while
# the old one is still on disk). ``discover_bookshelf_files`` must discover
# both, and their checkpoint markers/``source_file`` identity must not
# collapse to one -- the exact bug class ``discover_input_files``/
# ``input_key`` (checkpoint_markers.py) exist to fix, mirroring Task 9's
# ``discover_meta_files`` migration for pmc.
# --------------------------------------------------------------------------- #


def _bucket_archive_nxml(title: str, part_title: str, body_token: str, abstract_token: str) -> str:
    """A minimal real-shape (``<book-part-wrapper>`` root, see
    ``_REAL_SHAPE_NXML`` above) BITS-book NXML, parameterized so two archives
    sharing a basename can carry distinct, independently-verifiable content."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<book-part-wrapper id="toc" content-type="toc" dtd-version="2.0">
  <book-meta>
    <book-id book-id-type="pmcid">tr-{body_token}</book-id>
    <book-title-group>
      <book-title>{title}</book-title>
    </book-title-group>
    <pub-date publication-format="electronic" date-type="pub">
      <year>2023</year>
    </pub-date>
    <abstract>
      <title>Summary</title>
      <p>{abstract_token} book-level abstract text, long enough to clear the
      minimum ok-text-length threshold used by the extract status rule for
      this checkpoint-collision regression test fixture.</p>
    </abstract>
  </book-meta>
  <book-part book-part-type="section">
    <book-part-meta>
      <book-part-id book-part-id-type="pmcid">toc</book-part-id>
      <title-group>
        <title>{part_title}</title>
      </title-group>
    </book-part-meta>
    <body>
      <p>{body_token} part body text runs on at some length here so it
      comfortably clears the two-hundred character minimum the extract status
      rule checks for, covering fixture detail nobody will ever read closely
      but the extractor still has to hash, store and report on faithfully.</p>
    </body>
  </book-part>
</book-part-wrapper>
"""


def _write_bucket_archive(
    raw_dir: Path, rel_subdir: str, nxml: str, basename: str = "NBK1"
) -> Path:
    """Write ``<basename>.tar.gz`` (same basename every call by default) under
    ``raw_dir/packages/<rel_subdir>/``, mirroring
    ``download_bookshelf.sh``'s ``packages/<rel>`` layout."""
    pkg_dir = raw_dir / "packages" / rel_subdir
    pkg_dir.mkdir(parents=True, exist_ok=True)
    tar_path = pkg_dir / f"{basename}.tar.gz"
    data = nxml.encode("utf-8")
    with tarfile.open(tar_path, "w:gz") as tar:
        info = tarfile.TarInfo(name=f"{basename}/{basename}.nxml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return tar_path


def test_discover_bookshelf_files_keeps_distinct_paths_same_basename(tmp_path):
    """Regression: a naive rglob dedup keyed by basename would collapse two
    real ``packages/<bucket>/NBK1.tar.gz`` packages under different hash
    buckets into one discovered file. ``discover_bookshelf_files`` must
    discover both (dedup is by resolved full path, via
    ``checkpoint_markers.discover_input_files``)."""
    from episteme.data.bookshelf.extract_bookshelf import discover_bookshelf_files

    raw = tmp_path / "01_raw" / "bookshelf"
    tar_a = _write_bucket_archive(
        raw, "aa/11", _bucket_archive_nxml("Book A", "Part A", "TOKEN-A", "Bucket-A")
    )
    tar_b = _write_bucket_archive(
        raw, "bb/22", _bucket_archive_nxml("Book B", "Part B", "TOKEN-B", "Bucket-B")
    )

    files = discover_bookshelf_files(raw)

    assert len(files) == 2
    assert {f.resolve() for f in files} == {tar_a.resolve(), tar_b.resolve()}


def test_same_basename_different_buckets_both_processed_with_distinct_content(tmp_path):
    """Checkpoint-collision regression test (Task 10): two ``NBK1.tar.gz``
    packages with the *identical basename*, under different hash-bucket
    subdirectories of the real ``packages/`` tree, must each get their own
    discovery hit, checkpoint marker, and shard row -- not collapse into one,
    either at discovery (basename dedup) or at the success-marker check
    (basename identity).

    Also proves the stronger claim Task 9's review flagged marker-existence
    alone as insufficient for: each archive's OWN title/abstract/part-body
    text ends up attached to its OWN ``source_file``-keyed row, not the
    sibling bucket's -- i.e. no read/write cross-contamination between the
    two same-named archives.

    Note: because both archives are literally named ``NBK1.tar.gz``,
    ``_accession_from_filename`` derives the SAME accession ("NBK1") for
    both, so both book rows carry the SAME native id (``bookshelf:NBK1``)
    despite being distinct source archives with distinct content -- an
    id-under-a-different-source_file scenario ``postgres_loader.
    load_source_file``'s "(d0) id-collision guard" *touches* at load time,
    but does NOT resolve into two preserved books: (d0) deletes whichever
    row's source_file doesn't match the incoming shard's, so loading these
    two shards one after the other leaves only the LAST-loaded book's row in
    Postgres (last-writer-wins), not both -- see the Task 10 report for why
    that is a pre-existing, out-of-scope id-collision risk this task does
    not fix. This test covers only the extractor: both archives are
    discovered, processed independently, and produce independently correct
    content under distinct ``source_file`` keys and distinct shard files.
    """
    from episteme.data.bookshelf.extract_bookshelf import extract_bookshelf

    raw = tmp_path / "01_raw" / "bookshelf"
    _write_bucket_archive(
        raw, "aa/11", _bucket_archive_nxml("Book A", "Part A", "TOKEN-A", "Bucket-A")
    )
    _write_bucket_archive(
        raw, "bb/22", _bucket_archive_nxml("Book B", "Part B", "TOKEN-B", "Bucket-B")
    )

    processed = tmp_path / "02_processed"
    res = extract_bookshelf(raw, processed, workers=1)

    assert res["inputs"] == 2
    assert res["ok"] == 2
    assert res["failed"] == 0

    marks = sorted(p.name for p in (processed / "_ops" / "bookshelf" / "success").glob("*.ok"))
    assert len(marks) == 2
    assert marks[0] != marks[1]
    assert any("aa" in m and "11" in m for m in marks)
    assert any("bb" in m and "22" in m for m in marks)

    import polars as pl

    shard_dir = processed / "staging" / "bookshelf"
    shards = sorted(shard_dir.glob("*.*"))
    assert len(shards) == 2  # one shard per archive -- distinct source_file keys

    rows = []
    for shard in shards:
        rows.extend(
            (
                pl.read_parquet(shard) if shard.suffix == ".parquet" else pl.read_ndjson(shard)
            ).to_dicts()
        )

    books = [r for r in rows if r["container_id"] is None]
    parts = [r for r in rows if r["container_id"] is not None]
    assert len(books) == 2
    assert len(parts) == 2

    # Both books share the SAME native id (see docstring) -- the id-collision
    # this nested layout can produce, deliberately exercised here.
    assert {b["id"] for b in books} == {"bookshelf:NBK1"}

    # source_file distinguishes the two rows even though id does not, and
    # each carries its bucket subdirectory (never collapsing to bare "NBK1.tar.gz").
    source_files = {b["source_file"] for b in books}
    assert len(source_files) == 2
    assert any("aa" in sf and "11" in sf for sf in source_files)
    assert any("bb" in sf and "22" in sf for sf in source_files)

    by_source_file = {b["source_file"]: b for b in books}
    book_a = next(b for sf, b in by_source_file.items() if "aa" in sf)
    book_b = next(b for sf, b in by_source_file.items() if "bb" in sf)

    # No cross-contamination: each book's own title/abstract text, not its
    # sibling bucket's.
    assert book_a["title"] == "Book A"
    assert book_b["title"] == "Book B"
    assert "Bucket-A" in (book_a["text"] or "")
    assert "Bucket-B" not in (book_a["text"] or "")
    assert "Bucket-B" in (book_b["text"] or "")
    assert "Bucket-A" not in (book_b["text"] or "")

    parts_by_container_sf = {p["source_file"]: p for p in parts}
    part_a = next(p for sf, p in parts_by_container_sf.items() if "aa" in sf)
    part_b = next(p for sf, p in parts_by_container_sf.items() if "bb" in sf)
    assert part_a["container_id"] == "bookshelf:NBK1"
    assert part_b["container_id"] == "bookshelf:NBK1"
    assert part_a["title"] == "Part A"
    assert part_b["title"] == "Part B"
    assert "TOKEN-A" in (part_a["text"] or "")
    assert "TOKEN-B" not in (part_a["text"] or "")
    assert "TOKEN-B" in (part_b["text"] or "")
    assert "TOKEN-A" not in (part_b["text"] or "")


def test_same_basename_no_nbk_token_gets_distinct_ids(tmp_path):
    """Fallback-id regression (SP6/SP7 drift log close-out): when the archive
    filename carries NO ``NBK\\d+`` token, two archives sharing an identical
    basename in different subfolders must NOT collide on ``id`` (the
    ``postgres_loader`` (d0) id-collision guard would otherwise keep only the
    last-loaded one). The id must instead be derived from
    ``checkpoint_markers.input_key(path, raw_dir)``, which is unique per
    subfolder -- unlike the NBK-branch collision the sibling test above
    documents as a pre-existing, accepted, out-of-scope risk."""
    from episteme.data.bookshelf.extract_bookshelf import extract_bookshelf

    raw = tmp_path / "01_raw" / "bookshelf"
    _write_bucket_archive(
        raw,
        "aa/11",
        _bucket_archive_nxml("Book A", "Part A", "TOKEN-A", "Bucket-A"),
        basename="book",
    )
    _write_bucket_archive(
        raw,
        "bb/22",
        _bucket_archive_nxml("Book B", "Part B", "TOKEN-B", "Bucket-B"),
        basename="book",
    )

    processed = tmp_path / "02_processed"
    res = extract_bookshelf(raw, processed, workers=1)

    assert res["inputs"] == 2
    assert res["ok"] == 2
    assert res["failed"] == 0

    import polars as pl

    shard_dir = processed / "staging" / "bookshelf"
    shards = sorted(shard_dir.glob("*.*"))
    rows = []
    for shard in shards:
        rows.extend(
            (
                pl.read_parquet(shard) if shard.suffix == ".parquet" else pl.read_ndjson(shard)
            ).to_dicts()
        )

    books = [r for r in rows if r["container_id"] is None]
    parts = [r for r in rows if r["container_id"] is not None]
    assert len(books) == 2
    assert len(parts) == 2

    # No-NBK id collision must be resolved: two distinct book ids.
    book_ids = {b["id"] for b in books}
    assert len(book_ids) == 2, f"expected distinct ids, got {book_ids}"

    # Each part's container_id must point at its own book's id.
    by_id = {b["id"]: b for b in books}
    for p in parts:
        assert p["container_id"] in by_id

    part_ids = {p["id"] for p in parts}
    assert len(part_ids) == 2, f"expected distinct part ids, got {part_ids}"


def test_nbk_named_archive_id_unchanged(tmp_path):
    """The NBK branch of the fallback-id fix must not change: an
    NBK-accession-named archive keeps its ``bookshelf:<NBK...>`` id exactly
    as before."""
    from episteme.data.bookshelf.extract_bookshelf import extract_bookshelf

    raw_dir = _write_real_shape_archive(tmp_path / "src")
    out_dir = tmp_path / "out"
    res = extract_bookshelf(raw_dir, out_dir)
    assert res["ok"] == 1

    import polars as pl

    shard = sorted((out_dir / "staging" / "bookshelf").glob("*.*"))[0]
    rows = (
        pl.read_parquet(shard) if shard.suffix == ".parquet" else pl.read_ndjson(shard)
    ).to_dicts()
    book = [r for r in rows if r["container_id"] is None][0]
    assert book["id"] == "bookshelf:NBK599773"
