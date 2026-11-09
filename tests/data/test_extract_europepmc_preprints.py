"""europepmc_preprint extractor: EBI bulk files ``PPR<a>_PPR<b>.xml.gz`` -> bounded parts.

The fixtures are built here (small ``.xml.gz`` files in the shape of the bulk files:
one ``<articles>`` root, ``<article article-type="preprint">`` records). No DB, no network:
the audit row degrades to the JSONL mirror. ``not pg`` only.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from episteme.data import article_schema, parts_retire
from episteme.data.checkpoint_markers import (
    failed_marker_path,
    load_success_marker_path,
    success_marker_path,
)
from episteme.data.europepmc.preprints import extract_europepmc_preprints as ex

LIC_BY = "https://creativecommons.org/licenses/by/4.0/"
LIC_NC = "https://creativecommons.org/licenses/by-nc/4.0/"
LIC_ND = "https://creativecommons.org/licenses/by-nd/4.0/"
LIC_NCND = "https://creativecommons.org/licenses/by-nc-nd/4.0/"
TEXT = "Body paragraph about trial outcomes. " * 10


def article(
    ppr: str | None = "PPR1",
    *,
    doi: str | None = "10.1101/x1",
    abstract: str | None = "An abstract of the study.",
    body: str | None = TEXT,
    lic: str | None = LIC_BY,
    lic_type: str = "open-access",
) -> str:
    ids = '<article-id pub-id-type="manuscript">EMS1</article-id>'
    if doi:
        ids += f'<article-id pub-id-type="doi">{doi}</article-id>'
    if ppr:
        ids += f'<article-id pub-id-type="archive">{ppr}</article-id>'
    permissions = ""
    if lic:
        permissions = (
            f'<permissions><license license-type="{lic_type}" xlink:href="{lic}">'
            "<license-p>Licensed.</license-p></license></permissions>"
        )
    abs_xml = f"<abstract><p>{abstract}</p></abstract>" if abstract else ""
    body_xml = f"<body><p>{body}</p></body>" if body else ""
    return (
        '<article article-type="preprint" xml:lang="en"><front>'
        "<journal-meta><journal-title-group><journal-title>Preprints Journal"
        "</journal-title></journal-title-group></journal-meta>"
        f"<article-meta>{ids}<title-group><article-title>Title {ppr}</article-title></title-group>"
        '<contrib-group><contrib contrib-type="author"><name><surname>Okafor</surname>'
        "<given-names>Ada</given-names></name></contrib></contrib-group>"
        f'<pub-date pub-type="epub"><year>2026</year></pub-date>{permissions}{abs_xml}'
        f"</article-meta></front>{body_xml}</article>"
    )


def write_bulk(path: Path, articles: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<articles xmlns:xlink="http://www.w3.org/1999/xlink">' + "".join(articles) + "</articles>"
    )
    with gzip.open(path, "wb") as fh:
        fh.write(xml.encode("utf-8"))
    return path


def _rows(processed: Path):
    import polars as pl

    out = []
    for s in sorted((processed / "staging" / "europepmc_preprint").glob("*.parquet")):
        out.extend(pl.read_parquet(s).to_dicts())
    return out


def _caps(monkeypatch, rows: int, nbytes: int = 10**12) -> None:
    monkeypatch.setattr(ex, "_part_caps", lambda: (rows, nbytes))


def _many(n: int, start: int = 1) -> list[str]:
    return [article(f"PPR{i}", doi=f"10.1101/d{i}") for i in range(start, start + n)]


# ---------------------------------------------------------------- discovery
def test_discover_matches_only_bulk_names_in_id_order(tmp_path):
    for name in (
        "PPR208220_PPR297873.xml.gz",
        "PPR7001_PPR208218.xml.gz",
        "PPR726343.xml",
        "pprid.txt.gz",
        "PPR1_PPR2.xml.gz.part",
    ):
        (tmp_path / name).write_bytes(b"x")
    assert [p.name for p in ex.discover(tmp_path)] == [
        "PPR7001_PPR208218.xml.gz",
        "PPR208220_PPR297873.xml.gz",
    ]


# ------------------------------------------------------------------ streaming
def test_streams_a_fixture_with_several_articles(tmp_path):
    p = write_bulk(
        tmp_path / "PPR1_PPR3.xml.gz",
        [article("PPR1"), article("PPR2", doi="10.1101/x2"), article("PPR3", doi=None)],
    )
    rows = list(ex.iter_records(p))
    assert [r["source_record_id"] for r in rows] == ["PPR1", "PPR2", "PPR3"]
    assert {r["source"] for r in rows} == {"europepmc_preprint"}
    assert all(r["extract_status"] in article_schema.EXTRACT_STATUSES for r in rows)
    r = rows[0]
    assert r["title"] == "Title PPR1"
    assert r["authors"] == ["Ada Okafor"]
    assert r["year"] == 2026
    assert r["journal"] == "Preprints Journal"
    assert r["language"] == "en"
    assert r["body_text"].startswith("Body paragraph")
    assert r["container_id"] is None and r["book_meta"] is None and r["mesh"] is None


def test_reads_record_by_record_not_the_whole_file(tmp_path):
    """Two records are handed out before the (broken) rest of the file is read."""
    p = tmp_path / "PPR1_PPR9.xml.gz"
    xml = (
        '<?xml version="1.0"?><articles xmlns:xlink="http://www.w3.org/1999/xlink">'
        + article("PPR1")
        + article("PPR2", doi="10.1101/x2")
        + "<article><front><article-meta><article-id>"  # cut off: not well-formed
    )
    with gzip.open(p, "wb") as fh:
        fh.write(xml.encode())
    gen = ex.iter_records(p)
    assert next(gen)["source_record_id"] == "PPR1"
    assert next(gen)["source_record_id"] == "PPR2"
    with pytest.raises(Exception) as err:  # noqa: PT011 - the parser's own error class
        next(gen)
    assert "Parse" in type(err.value).__name__ or "XML" in type(err.value).__name__


# ----------------------------------------------------------------- identity
def test_identity_doi_form_and_ppr_form(tmp_path):
    p = write_bulk(
        tmp_path / "PPR1_PPR2.xml.gz",
        [article("PPR1", doi="10.1101/abc"), article("PPR2", doi=None)],
    )
    a, b = ex.iter_records(p)
    # the id form of the earlier per-id extractor: doi first, else source:PPR
    assert a["id"] == "doi:10.1101/abc"
    assert b["id"] == "europepmc_preprint:PPR2"
    assert a["source_record_id"] == "PPR1"
    assert a["doi"] == "10.1101/abc" and b["doi"] is None


@pytest.mark.parametrize(
    ("lic", "code", "subset"),
    [
        (LIC_BY, "CC BY", "commercial"),
        (LIC_ND, "CC BY-ND", "commercial"),
        (LIC_NC, "CC BY-NC", "text_mining"),
        (LIC_NCND, "CC BY-NC-ND", "text_mining"),
        (None, "unknown", "open_metadata"),
    ],
)
def test_licence_and_subset(tmp_path, lic, code, subset):
    p = write_bulk(tmp_path / "PPR1_PPR1.xml.gz", [article("PPR1", lic=lic)])
    (row,) = ex.iter_records(p)
    assert row["license"] == code
    assert row["subset"] == subset
    if lic:
        assert row["license_url"] == lic


# ------------------------------------------------------------------ skipping
def test_record_without_ppr_id_is_counted_and_skipped(tmp_path):
    p = write_bulk(
        tmp_path / "PPR1_PPR3.xml.gz",
        [article("PPR1"), article(None, doi="10.1101/nop"), article("PPR3", doi="10.1101/x3")],
    )
    skips = ex.Counter()
    rows = list(ex.iter_records(p, skips))
    assert [r["source_record_id"] for r in rows] == ["PPR1", "PPR3"]
    assert skips["skipped_no_ppr_id"] == 1


def test_record_without_body_and_abstract_is_counted_and_skipped(tmp_path):
    p = write_bulk(
        tmp_path / "PPR1_PPR4.xml.gz",
        [
            article("PPR1", abstract=None, body=None),
            article("PPR2", doi="10.1101/x2", abstract=None),  # body only: kept
            article("PPR3", doi="10.1101/x3", body=None),  # abstract only: kept
            article("PPR4", doi="10.1101/x1"),  # same id as nothing yet
        ],
    )
    skips = ex.Counter()
    rows = list(ex.iter_records(p, skips))
    assert [r["source_record_id"] for r in rows] == ["PPR2", "PPR3", "PPR4"]
    assert skips["skipped_no_text"] == 1


def test_repeated_id_in_a_file_keeps_the_first(tmp_path):
    p = write_bulk(tmp_path / "PPR1_PPR2.xml.gz", [article("PPR1"), article("PPR2")])
    skips = ex.Counter()
    rows = list(ex.iter_records(p, skips))  # both have doi 10.1101/x1
    assert [r["source_record_id"] for r in rows] == ["PPR1"]
    assert skips["skipped_duplicate_id"] == 1


# ------------------------------------------------------ parts, resume, retire
def test_unit_is_written_in_bounded_parts_and_counts_skips(tmp_path, monkeypatch):
    _caps(monkeypatch, 2)
    raw, proc = tmp_path / "raw", tmp_path / "proc"
    write_bulk(raw / "PPR1_PPR9.xml.gz", [*_many(5), article(None, doi="10.1101/nop")])
    res = ex.extract_europepmc_preprints(raw, proc)
    assert (res["inputs"], res["ok"], res["failed"], res["rows"]) == (1, 1, 0, 5)
    assert res["skipped_no_ppr_id"] == 1
    rows = _rows(proc)
    assert len(rows) == 5
    assert {r["source_file"] for r in rows} == {
        "PPR1_PPR9__part00001",
        "PPR1_PPR9__part00002",
        "PPR1_PPR9__part00003",
    }
    marker = json.loads(success_marker_path(proc, "europepmc_preprint", "PPR1_PPR9").read_text())
    st = marker["stats"]
    assert st["parts"] == 3 and st["n_rows"] == 5 and st["skipped_no_ppr_id"] == 1
    assert st["parts_list"] == [f"PPR1_PPR9__part0000{i}" for i in (1, 2, 3)]
    assert (
        max(
            sum(1 for r in rows if r["source_file"] == f"PPR1_PPR9__part0000{i}") for i in (1, 2, 3)
        )
        == 2
    )


def test_byte_cap_starts_the_next_part(tmp_path, monkeypatch):
    _caps(monkeypatch, 1000, 1)  # every row is over the cap: one row per part
    raw, proc = tmp_path / "raw", tmp_path / "proc"
    write_bulk(raw / "PPR1_PPR9.xml.gz", _many(3))
    assert ex.extract_europepmc_preprints(raw, proc)["rows"] == 3
    assert len({r["source_file"] for r in _rows(proc)}) == 3


def test_resume_skips_an_unchanged_unit_and_force_rewrites(tmp_path, monkeypatch):
    _caps(monkeypatch, 2)
    raw, proc = tmp_path / "raw", tmp_path / "proc"
    write_bulk(raw / "PPR1_PPR9.xml.gz", _many(3))
    write_bulk(raw / "PPR10_PPR19.xml.gz", _many(2, start=10))
    first = ex.extract_europepmc_preprints(raw, proc)
    assert first["rows"] == 5
    again = ex.extract_europepmc_preprints(raw, proc)
    assert (again["ok"], again["failed"], again["rows"]) == (0, 0, 0)  # both skipped
    forced = ex.extract_europepmc_preprints(raw, proc, force=True)
    assert forced["rows"] == 5
    assert len(_rows(proc)) == 5  # rewritten, not doubled


def test_changed_unit_is_rewritten_and_loaded_parts_go_to_retire(tmp_path, monkeypatch):
    _caps(monkeypatch, 2)
    raw, proc = tmp_path / "raw", tmp_path / "proc"
    f = write_bulk(raw / "PPR1_PPR9.xml.gz", _many(5))  # 3 parts
    ex.extract_europepmc_preprints(raw, proc)
    # parts 2 and 3 were loaded
    for n in (2, 3):
        m = load_success_marker_path(proc, "europepmc_preprint", f"PPR1_PPR9__part0000{n}.parquet")
        m.parent.mkdir(parents=True, exist_ok=True)
        m.write_text("{}")
    write_bulk(f, _many(2))  # the file shrinks: one part
    res = ex.extract_europepmc_preprints(raw, proc)
    assert res["rows"] == 2
    assert {r["source_file"] for r in _rows(proc)} == {"PPR1_PPR9__part00001"}
    assert parts_retire.read_retire_pending(proc, "europepmc_preprint") == [
        "PPR1_PPR9__part00002",
        "PPR1_PPR9__part00003",
    ]


def test_unparseable_unit_fails_with_a_marker_and_no_unit_marker(tmp_path):
    raw, proc = tmp_path / "raw", tmp_path / "proc"
    raw.mkdir()
    with gzip.open(raw / "PPR1_PPR9.xml.gz", "wb") as fh:
        fh.write(b"<articles><article>")
    res = ex.extract_europepmc_preprints(raw, proc)
    assert res["failed"] == 1 and res["ok"] == 0
    assert failed_marker_path(proc, "europepmc_preprint", "PPR1_PPR9").is_file()
    assert not success_marker_path(proc, "europepmc_preprint", "PPR1_PPR9").exists()


def test_max_files_limits_units(tmp_path):
    raw, proc = tmp_path / "raw", tmp_path / "proc"
    write_bulk(raw / "PPR1_PPR9.xml.gz", _many(2))
    write_bulk(raw / "PPR10_PPR19.xml.gz", _many(2, start=10))
    assert ex.extract_europepmc_preprints(raw, proc, max_files=1)["inputs"] == 1


# ----------------------------------------------------------- input_key rule
def test_same_basename_in_two_directories_gets_two_unit_keys(tmp_path):
    a = write_bulk(tmp_path / "raw" / "a" / "PPR1_PPR9.xml.gz", _many(1))
    b = write_bulk(tmp_path / "raw" / "b" / "PPR1_PPR9.xml.gz", _many(1, start=50))
    raw = tmp_path / "raw"
    ka, kb = ex.unit_key(a, raw), ex.unit_key(b, raw)
    assert ka != kb
    assert ka == "a__PPR1_PPR9" and kb == "b__PPR1_PPR9"
    # a flat file keeps its bare stem
    assert ex.unit_key(raw / "PPR1_PPR9.xml.gz", raw) == "PPR1_PPR9"
    # the part names (stored source_file) differ too
    assert ex.part_name(ka, 1) != ex.part_name(kb, 1)


def test_archive_typed_id_is_preferred_and_value_match_is_the_fallback(tmp_path):
    both = article("PPR5").replace(
        '<article-id pub-id-type="archive">PPR5</article-id>',
        '<article-id pub-id-type="other">PPR999</article-id>'
        '<article-id pub-id-type="archive">PPR5</article-id>',
    )
    other = article("PPR6", doi="10.1101/x6").replace('pub-id-type="archive"', 'pub-id-type="ppr"')
    p = write_bulk(tmp_path / "PPR5_PPR6.xml.gz", [both, other])
    assert [r["source_record_id"] for r in ex.iter_records(p)] == ["PPR5", "PPR6"]
