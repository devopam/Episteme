import json
from pathlib import Path

from episteme.data.pmc.extract_pmc import discover_meta_files, extract_pmc

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


def test_same_basename_different_subdirs_both_processed(tmp_path):
    """Checkpoint-collision regression test (Task 9): two metadata files
    with the *same basename* (``PMC1.json``) in different subdirectories
    under raw_dir must each get their own discovery hit, checkpoint
    marker, and row -- not collapse into one, either at discovery
    (basename dedup) or at the success-marker check (basename identity).

    Also proves that, for this colocated-XML layout, the same-name
    collision does not cross-contaminate XML resolution: each metadata
    file's colocated XML (``meta_path.with_suffix(".xml")``, the fallback
    candidate in ``find_xml_for_meta``) is matched to its own row, not the
    sibling directory's file. This is NOT the same as proving XML
    resolution is collision-safe in general: ``find_xml_for_meta`` also
    tries ``raw_dir / "xml" / f"{version_id}.xml"`` (a single shared
    directory keyed only by ``version_id = meta_path.stem``, which two
    same-basename metas share), so a raw_dir carrying that layout instead
    -- e.g. a real ``download_pmc.py`` output tree merged from two runs,
    both dropping XML into the one shared ``xml/`` dir -- would still
    resolve both same-named metas to whichever one XML file happens to be
    there. That is a separate, pre-existing ambiguity in
    ``find_xml_for_meta``'s shared-``xml/``-dir candidates, not something
    this task's basename-identity migration introduces or fixes; see the
    task-9 report for the full reasoning.
    """
    raw = tmp_path / "01_raw" / "pmc" / "oa_comm"
    dir_a = raw / "metadata" / "batch_a"
    dir_b = raw / "metadata" / "batch_b"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)

    meta_a = dir_a / "PMC1.json"
    meta_b = dir_b / "PMC1.json"
    xml_a = dir_a / "PMC1.xml"
    xml_b = dir_b / "PMC1.xml"

    meta_a.write_text(_meta("PMC1", 40000001, "10.0/a"), encoding="utf-8")
    meta_b.write_text(_meta("PMC2", 40000002, "10.0/b"), encoding="utf-8")
    xml_a.write_text(_xml("PMC1", 40000001, "10.0/a"), encoding="utf-8")
    xml_b.write_text(_xml("PMC2", 40000002, "10.0/b"), encoding="utf-8")

    processed = tmp_path / "02_processed"
    res = extract_pmc(raw, processed, workers=1)

    assert res["inputs"] == 2
    assert res["ok"] == 2
    assert res["failed"] == 0

    marks = sorted(p.name for p in (processed / "_ops" / "pmc" / "success").glob("*.ok"))
    assert len(marks) == 2
    assert marks[0] != marks[1]
    assert any("batch_a" in m for m in marks)
    assert any("batch_b" in m for m in marks)

    shard_dir = processed / "staging" / "pmc"
    rows = []
    for shard in shard_dir.glob("*"):
        if shard.suffix == ".parquet":
            import pyarrow.parquet as pq

            rows.extend(pq.read_table(shard).to_pylist())
        else:
            rows.extend(json.loads(line) for line in shard.read_text().splitlines())

    pmcids = sorted(r["pmcid"] for r in rows)
    assert pmcids == ["PMC1", "PMC2"]
    by_pmcid = {r["pmcid"]: r for r in rows}
    # No XML cross-contamination: each row's pmid/doi must come from its own
    # directory's metadata+xml pair, not the sibling same-basename file's.
    assert by_pmcid["PMC1"]["pmid"] == "40000001"
    assert by_pmcid["PMC2"]["pmid"] == "40000002"
    assert by_pmcid["PMC1"]["doi"] == "10.0/a"
    assert by_pmcid["PMC2"]["doi"] == "10.0/b"
