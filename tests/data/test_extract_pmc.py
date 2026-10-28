import json
import logging
from pathlib import Path

from episteme.data.pmc.extract_pmc import discover_meta_files, extract_pmc, find_xml_for_meta

FIX = Path(__file__).parent / "fixtures" / "pmc"


def test_extract_pmc_produces_one_ok_row(tmp_path):
    processed = tmp_path / "02_processed"
    res = extract_pmc(FIX, processed, max_files=1)
    assert res["inputs"] == 1
    assert res["ok"] == 1
    assert res["failed"] == 0
    assert res["rows"] >= 1
    # success marker written
    marks = list((processed / "_ops" / "pmc" / "success").glob("*.ok"))
    assert marks
    # staging shard written
    shards = list((processed / "staging" / "pmc").glob("*"))
    assert shards


def test_extract_pmc_row_fields(tmp_path):
    import json

    processed = tmp_path / "02_processed"
    extract_pmc(FIX, processed, max_files=1)
    shard = next((processed / "staging" / "pmc").glob("*"))
    # read the shard back (parquet or jsonl)
    if shard.suffix == ".parquet":
        import pyarrow.parquet as pq

        rows = pq.read_table(shard).to_pylist()
    else:
        rows = [json.loads(line) for line in shard.read_text().splitlines()]
    r = rows[0]
    assert r["source"] == "pmc"
    assert r["pmcid"] == "PMCFIX0001"
    assert "acetylcholinesterase" in (r["text"] or "").lower()
    assert r["license"] == "CC BY"
    assert r["subset"] == "commercial"
    assert r["extract_status"] == "ok"


def _meta(pmcid: str, pmid: int, doi: str) -> str:
    return json.dumps(
        {
            "pmcid": pmcid,
            "version": 1,
            "pmid": pmid,
            "doi": doi,
            "title": f"Article {pmcid}",
            "citation": "Test J. 2024 Jan 1;1(1):1-2.",
            "is_manuscript": False,
            "is_historical_ocr": False,
            "is_retracted": False,
            "license_code": "CC BY",
        }
    )


def _xml(pmcid: str, pmid: int, doi: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink" article-type="research-article">
  <front>
    <journal-meta>
      <journal-title-group><journal-title>Test Journal</journal-title></journal-title-group>
    </journal-meta>
    <article-meta>
      <article-id pub-id-type="pmc">{pmcid}</article-id>
      <article-id pub-id-type="pmid">{pmid}</article-id>
      <article-id pub-id-type="doi">{doi}</article-id>
      <title-group><article-title>Article {pmcid}</article-title></title-group>
      <pub-date pub-type="epub"><year>2024</year></pub-date>
      <permissions>
        <license xlink:href="https://creativecommons.org/licenses/by/4.0/">
          <license-p>This is an open access article under the CC BY 4.0 license.</license-p>
        </license>
      </permissions>
      <abstract><p>Abstract text for {pmcid}, long enough to exceed the minimum ok text
      length threshold so extract_status lands ok, not partial, for this
      checkpoint-collision regression test.</p></abstract>
    </article-meta>
  </front>
  <body><sec><title>Introduction</title><p>Body text for {pmcid}.</p></sec></body>
</article>"""


def test_discover_meta_files_keeps_distinct_paths_same_basename(tmp_path):
    """Regression: the old discover_meta_files deduped by *basename* --
    two files literally named ``PMC1.json`` in different subdirectories
    would collapse to a single discovered file. discover_meta_files must
    now discover both (dedup is by resolved full path)."""
    raw = tmp_path / "01_raw" / "pmc" / "oa_comm"
    dir_a = raw / "metadata" / "batch_a"
    dir_b = raw / "metadata" / "batch_b"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    meta_a = dir_a / "PMC1.json"
    meta_b = dir_b / "PMC1.json"
    meta_a.write_text(_meta("PMC1", 1, "10.0/a"), encoding="utf-8")
    meta_b.write_text(_meta("PMC2", 2, "10.0/b"), encoding="utf-8")

    files = discover_meta_files(raw)

    assert len(files) == 2
    assert {f.resolve() for f in files} == {meta_a.resolve(), meta_b.resolve()}


def _read_staging(processed: Path) -> list[dict]:
    rows = []
    for shard in (processed / "staging" / "pmc").glob("*"):
        if shard.suffix == ".parquet":
            import pyarrow.parquet as pq

            rows.extend(pq.read_table(shard).to_pylist())
        else:
            rows.extend(json.loads(line) for line in shard.read_text().splitlines())
    return rows


def test_articles_in_different_subdirs_each_attach_their_own_xml(tmp_path):
    """Checkpoint-collision regression test (Task 9), bucket layout: two
    articles in different subdirectories under raw_dir are both extracted,
    and each row takes its pmid/doi from its own colocated XML (the FIRST,
    most specific, candidate of ``find_xml_for_meta``), not the sibling
    directory's file."""
    raw = tmp_path / "01_raw" / "pmc" / "oa_comm"
    dir_a = raw / "metadata" / "batch_a"
    dir_b = raw / "metadata" / "batch_b"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)

    (dir_a / "PMC1.1.json").write_text(_meta("PMC1", 40000001, "10.0/a"), encoding="utf-8")
    (dir_b / "PMC2.1.json").write_text(_meta("PMC2", 40000002, "10.0/b"), encoding="utf-8")
    (dir_a / "PMC1.1.xml").write_text(_xml("PMC1", 40000001, "10.0/a"), encoding="utf-8")
    (dir_b / "PMC2.1.xml").write_text(_xml("PMC2", 40000002, "10.0/b"), encoding="utf-8")

    processed = tmp_path / "02_processed"
    res = extract_pmc(raw, processed, workers=1)

    assert (res["inputs"], res["ok"], res["failed"], res["rows"]) == (1, 1, 0, 2)
    rows = _read_staging(processed)
    assert sorted(r["pmcid"] for r in rows) == ["PMC1", "PMC2"]
    assert {r["source_file"] for r in rows} == {"pmc_b0000__part00001"}
    by_pmcid = {r["pmcid"]: r for r in rows}
    assert by_pmcid["PMC1"]["pmid"] == "40000001"
    assert by_pmcid["PMC2"]["pmid"] == "40000002"
    assert by_pmcid["PMC1"]["doi"] == "10.0/a"
    assert by_pmcid["PMC2"]["doi"] == "10.0/b"


def test_same_basename_different_subdirs_both_considered_one_kept(tmp_path):
    """Two metadata files with the same basename (``PMC1.1.json``) in
    different subdirectories are both discovered and considered (never
    collapsed by basename). They are the same article and version, so the
    bucket keeps one -- the first by input_key -- and counts the other in
    ``duplicate_files``. The kept row takes its XML from its own folder."""
    raw = tmp_path / "01_raw" / "pmc" / "oa_comm"
    dir_a = raw / "metadata" / "batch_a"
    dir_b = raw / "metadata" / "batch_b"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    (dir_a / "PMC1.1.json").write_text(_meta("PMC1", 40000001, "10.0/a"), encoding="utf-8")
    (dir_b / "PMC1.1.json").write_text(_meta("PMC1", 40000002, "10.0/b"), encoding="utf-8")
    (dir_a / "PMC1.1.xml").write_text(_xml("PMC1", 40000001, "10.0/a"), encoding="utf-8")
    (dir_b / "PMC1.1.xml").write_text(_xml("PMC1", 40000002, "10.0/b"), encoding="utf-8")

    assert len(discover_meta_files(raw)) == 2
    processed = tmp_path / "02_processed"
    res = extract_pmc(raw, processed, workers=1)
    assert (res["rows"], res["duplicate_files"]) == (1, 1)
    (row,) = _read_staging(processed)
    assert (row["pmid"], row["doi"]) == ("40000001", "10.0/a")


# --------------------------------------------------------------------------- #
# find_xml_for_meta ordering regression (SP6/SP7 drift log close-out)
# --------------------------------------------------------------------------- #


def test_find_xml_for_meta_prefers_colocated_over_shared(tmp_path):
    """Colocated XML (meta_path.with_suffix('.xml')) must win over a shared
    raw_dir/xml/<version_id>.xml, otherwise two different metadata files
    sharing the same version_id could both attach the same shared XML --
    the most specific candidate must be checked first."""
    raw = tmp_path / "raw"
    batch_dir = raw / "batchA"
    batch_dir.mkdir(parents=True)
    meta_path = batch_dir / "PMC1.1.json"
    meta_path.write_text(_meta("PMC1", 111, "10.1/a"), encoding="utf-8")

    colocated_xml = batch_dir / "PMC1.1.xml"
    colocated_xml.write_text(_xml("PMC1", 111, "10.1/a"), encoding="utf-8")

    # A shared raw_dir/xml/<version_id>.xml with DIFFERENT content -- must
    # lose to the colocated file above.
    shared_dir = raw / "xml"
    shared_dir.mkdir(parents=True)
    shared_xml = shared_dir / "PMC1.1.xml"
    shared_xml.write_text(_xml("PMC1", 999, "10.1/shared"), encoding="utf-8")

    result = find_xml_for_meta(meta_path, raw, "PMC1.1")

    assert result == colocated_xml


def test_find_xml_for_meta_ambiguous_rglob_returns_none_and_logs_warning(tmp_path, caplog):
    raw = tmp_path / "raw"
    meta_dir = raw / "batchA"
    meta_dir.mkdir(parents=True)
    meta_path = meta_dir / "PMC1.1.json"
    meta_path.write_text(_meta("PMC1", 111, "10.1/a"), encoding="utf-8")

    # No colocated, no batch-own, no shared raw_dir/xml or raw_dir/xml/all
    # candidate -- only two ambiguous matches deeper under raw_dir/xml.
    sub_a = raw / "xml" / "subA"
    sub_b = raw / "xml" / "subB"
    sub_a.mkdir(parents=True)
    sub_b.mkdir(parents=True)
    (sub_a / "PMC1.1.xml").write_text(_xml("PMC1", 111, "10.1/a"), encoding="utf-8")
    (sub_b / "PMC1.1.xml").write_text(_xml("PMC1", 222, "10.1/b"), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="episteme.data.pmc.extract_pmc"):
        result = find_xml_for_meta(meta_path, raw, "PMC1.1")

    assert result is None
    warnings = [
        r
        for r in caplog.records
        if r.name == "episteme.data.pmc.extract_pmc" and r.levelno == logging.WARNING
    ]
    assert warnings, "expected a WARNING logged for the ambiguous rglob match"
    assert "PMC1.1" in warnings[0].getMessage()
