"""The sample slices of the regulatory sources: FAERS, DailyMed, EMA feeds, EPAR, openFDA.

Files are small stand-ins with the real names and layout. No test touches a database or
the network.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from episteme.data import bucket_parts
from episteme.data.dailymed.extract_dailymed import discover_outer_zips, release_part_gaps
from episteme.data.regulatory.faers.generation import faers_zips
from episteme.ops import gate_slice

KINDS = ("prescription", "otc")
PREFIX = "https://www.ema.europa.eu/"  # the sources.env value of EMA_EPAR_DOCUMENT_PREFIX


@pytest.fixture(autouse=True)
def _fresh_settings():
    import episteme.config as cfg

    cfg.get_settings.cache_clear()
    yield
    cfg.get_settings.cache_clear()


def _w(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


# ---------------------------------------------------------------------------
# FAERS
# ---------------------------------------------------------------------------
def _faers(raw: Path, quarter: str, part: int, of: int, size: int) -> Path:
    name = f"drug-event-{part:04d}-of-{of:04d}.json.zip"
    return _w(raw / "drug" / "event" / quarter / name, size)


def _faers_raw(tmp_path) -> Path:
    raw = tmp_path / "raw" / "openfda"
    # Quarters 2004q1 .. 2024q4 would be too many files; a set with every case in it.
    for q, sizes in {
        "2004q1": [10, 12],
        "2011q4": [30],
        "2012q2": [31, 32],  # 2012q3 is absent: the nearest quarter is 2012q2 (or 2012q4)
        "2012q4": [40],
        "2020q1": [100, 100],
        "2020q2": [100, 100],
        "2020q3": [100, 100],
        "2020q4": [100, 100],  # 2020 holds 800 bytes: the largest year
        "2023q1": [50, 51],
        "2024q4": [60, 90, 70],  # the newest quarter: the largest file is 90
    }.items():
        for i, s in enumerate(sizes, 1):
            _faers(raw, q, i, len(sizes), s)
    _w(raw / "drug" / "event" / "all_other" / "drug-event-0001-of-0001.json.zip", 5000)
    _w(raw / "drug" / "ndc" / "drug-ndc-0001-of-0001.json.zip", 5000)
    return raw


def _quarters(files: list[Path]) -> set[str]:
    return {p.parent.name for p in files}


def test_faers_slice_has_three_eras(tmp_path):
    raw = _faers_raw(tmp_path)
    got = gate_slice._select_faers(raw)
    quarters = _quarters(got)
    assert "2004q1" in quarters  # the oldest quarter (the AERS era)
    assert "2012q2" in quarters or "2012q4" in quarters  # the nearest to 2012q3
    assert "2024q4" in quarters  # the newest quarter
    # The oldest quarter whole; the newest quarter only its largest file.
    assert {p.name for p in got if p.parent.name == "2004q1"} == {
        "drug-event-0001-of-0002.json.zip",
        "drug-event-0002-of-0002.json.zip",
    }
    newest = [p for p in got if p.parent.name == "2024q4"]
    assert [p.name for p in newest] == ["drug-event-0002-of-0003.json.zip"]
    # Nothing outside drug/event/<year>q<n>/.
    assert "all_other" not in quarters
    assert all(p.parent.parent.name == "event" for p in got)
    assert len(set(got)) == len(got)
    assert gate_slice._select_faers(raw) == got  # deterministic


def test_faers_slice_takes_the_quarter_nearest_to_2012q3(tmp_path):
    raw = _faers_raw(tmp_path)
    got = gate_slice._select_faers(raw)
    # 2012q2 and 2012q4 are one quarter from 2012q3: the earlier one wins a tie.
    assert "2012q2" in _quarters(got)
    assert "2012q4" not in _quarters(got)
    # With 2012q3 present, it is the one.
    for i in (1,):
        _faers(raw, "2012q3", i, 1, 33)
    assert "2012q3" in _quarters(gate_slice._select_faers(raw))
    assert "2012q2" not in _quarters(gate_slice._select_faers(raw))


def test_faers_slice_holds_the_largest_year_whole(tmp_path):
    """The signal build runs by year: the slice must hold the largest year of the raw set
    (here 2020, four quarters), so the sample builds the signals of a year-sized partition."""
    raw = _faers_raw(tmp_path)
    got = gate_slice._select_faers(raw)
    year_2020 = [p for p in got if p.parent.name.startswith("2020")]
    assert len(year_2020) == 8
    assert gate_slice.faers_largest_year(raw) == 2020


def test_faers_slice_lists_a_file_once_and_passes_the_input_set_reader(tmp_path):
    raw = tmp_path / "raw" / "openfda"
    _faers(raw, "2012q3", 1, 1, 10)  # the oldest, the nearest and the newest quarter at once
    got = gate_slice._select_faers(raw)
    assert got == [raw / "drug" / "event" / "2012q3" / "drug-event-0001-of-0001.json.zip"]
    assert faers_zips(raw) == got


def test_faers_slice_without_a_quarter_is_refused(tmp_path):
    raw = tmp_path / "raw" / "openfda"
    _w(raw / "drug" / "event" / "all_other" / "drug-event-0001-of-0001.json.zip", 5)
    with pytest.raises(gate_slice.SliceError, match="openfda_faers"):
        gate_slice._select_faers(raw)


def test_faers_slice_is_registered_on_the_openfda_folder_with_a_32_gb_default_cap():
    spec = gate_slice.SLICES["openfda_faers"]
    assert spec.raw_subpath == Path("openfda")
    assert spec.max_bytes == 32 * gate_slice.GB  # the whole raw set is about 105 GB


def test_faers_slice_over_its_cap_is_refused_and_names_the_key(tmp_path, monkeypatch):
    raw_root = tmp_path / "raw"
    _faers(raw_root / "openfda", "2012q3", 1, 1, 100)
    processed = tmp_path / "proc"
    sample_root = processed / "_sample" / "openfda_faers"
    monkeypatch.setenv("EPISTEME_SLICE_MAX_BYTES_OPENFDA_FAERS", "10")
    import episteme.config as cfg

    cfg.get_settings.cache_clear()
    with pytest.raises(gate_slice.SliceError) as exc:
        gate_slice.prepare("openfda_faers", raw_root, sample_root, processed_root=processed)
    assert "over the cap of 10" in str(exc.value)
    assert "EPISTEME_SLICE_MAX_BYTES_OPENFDA_FAERS" in str(exc.value)


# ---------------------------------------------------------------------------
# openFDA feeds
# ---------------------------------------------------------------------------
def test_openfda_slice_takes_the_feed_zips_only(tmp_path):
    raw = tmp_path / "raw" / "openfda"
    want = [
        _w(raw / "drug" / "ndc" / "drug-ndc-0001-of-0001.json.zip", 5),
        _w(raw / "drug" / "enforcement" / "drug-enforcement-0001-of-0001.json.zip", 5),
        _w(raw / "drug" / "drugsfda" / "drug-drugsfda-0001-of-0001.json.zip", 5),
    ]
    _faers(raw, "2024q4", 1, 1, 5)
    _w(raw / "drug" / "label" / "drug-label-0001-of-0001.json.zip", 5)
    _w(raw / "device" / "enforcement" / "device-enforcement-0001-of-0001.json.zip", 5)
    _w(raw / "other" / "substance" / "other-substance-0001-of-0001.json.zip", 5)
    got = gate_slice._select_openfda(raw)
    assert sorted(got) == sorted(want)
    assert gate_slice.SLICES["openfda"].raw_subpath == Path("openfda")


# ---------------------------------------------------------------------------
# EMA feeds
# ---------------------------------------------------------------------------
def test_ema_slice_takes_every_feed_file_and_no_pdf(tmp_path):
    raw = tmp_path / "raw" / "ema"
    feeds = [
        _w(raw / "ema_medicines.json", 5),
        _w(raw / "ema_herbal.json", 5),
        _w(raw / "ema_article57.xlsx", 5),
        _w(raw / "ema_epar_documents.json", 5),
    ]
    _w(raw / "epar" / "1.pdf", 5)
    _w(raw / "ema_not_a_feed.json", 5)
    got = gate_slice._select_ema(raw)
    assert sorted(got) == sorted(feeds)
    assert gate_slice.SLICES["ema"].raw_subpath == Path("ema")


# ---------------------------------------------------------------------------
# DailyMed
# ---------------------------------------------------------------------------
NDC = "2.16.840.1.113883.6.69"


def _xml(set_id: str, padding: int = 0, products: int = 1) -> bytes:
    codes = "".join(
        f'<containerPackagedProduct><code code="{10000 + i}-{100 + i}-01" codeSystem="{NDC}"/>'
        "</containerPackagedProduct>"
        for i in range(products)
    )
    pad = "<!--" + "p" * padding + "-->" if padding else ""
    return (
        '<document xmlns="urn:hl7-org:v3">'
        f'<setId root="{set_id}"/><versionNumber value="1"/>{pad}{codes}</document>'
    ).encode()


def _inner(xml: bytes, images: int = 0) -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("label.xml", xml)
        if images:
            zf.writestr("fig.jpg", b"\x01" * images, zipfile.ZIP_STORED)
    return buf.getvalue()


def _outer(path: Path, members: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _sid(n: int) -> str:
    return f"{n:08x}-aaaa-4bbb-8ccc-{n:012x}"


@pytest.fixture
def dailymed_raw(tmp_path) -> Path:
    raw = tmp_path / "raw" / "dailymed"
    # Rx part 1: label 1 has the largest XML; label 2 lists the most products.
    _outer(
        raw / "dm_spl_release_human_rx_part1.zip",
        {
            "prescription/20240101_a1.zip": _inner(_xml(_sid(1), padding=50_000)),
            "prescription/20240101_a2.zip": _inner(_xml(_sid(2), padding=2_000, products=40)),
            "prescription/20240101_a3.zip": _inner(_xml(_sid(3), padding=1_000)),
        },
    )
    # Rx part 2: a large image makes one zip big, with a small XML.
    _outer(
        raw / "dm_spl_release_human_rx_part2.zip",
        {"prescription/20240101_b1.zip": _inner(_xml(_sid(4), padding=100), images=300_000)},
    )
    _outer(
        raw / "dm_spl_release_human_otc_part1.zip",
        {
            "otc/20240101_c1.zip": _inner(_xml(_sid(5), padding=100)),
            "otc/20240101_c2.zip": _inner(_xml(_sid(6), padding=3_000)),
            "otc/20240101_c3.zip": _inner(_xml(_sid(7), padding=9_000)),
        },
    )
    # An update and an animal release are not part of the slice.
    _outer(raw / "dm_spl_weekly_update_20240101.zip", {"prescription/x.zip": _inner(_xml(_sid(8)))})
    _outer(raw / "dm_spl_release_animal_part1.zip", {"other/y.zip": _inner(_xml(_sid(9)))})
    return raw


def _members(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        return sorted(zf.namelist())


def test_dailymed_slice_has_the_largest_label(dailymed_raw, tmp_path):
    dest = tmp_path / "slice" / "dailymed"
    meta = gate_slice.SLICES["dailymed"].build(dailymed_raw, dest)
    rx = dest / "dm_spl_release_human_rx_part1.zip"
    otc = dest / "dm_spl_release_human_otc_part1.zip"
    rx_members = _members(rx)
    # The largest XML (a1) and the label with the most products (a2). The label with the
    # largest zip (b1, by its image) has a small XML and is not picked for its size.
    assert rx_members == ["prescription/20240101_a1.zip", "prescription/20240101_a2.zip"]
    assert len(_members(otc)) == 1 and _members(otc)[0].startswith("otc/")
    assert not (dest / "dm_spl_weekly_update_20240101.zip").exists()
    assert sorted(meta["files"]) == [otc.name, rx.name]
    assert sorted(meta["inputs"]) == [otc.name, rx.name]
    assert meta["bytes"] == rx.stat().st_size + otc.stat().st_size
    # The inner zip bytes and member names are the production ones.
    with zipfile.ZipFile(rx) as new, zipfile.ZipFile(dailymed_raw / rx.name) as old:
        assert new.read("prescription/20240101_a1.zip") == old.read("prescription/20240101_a1.zip")


def test_dailymed_slice_is_a_valid_input_of_the_extract(dailymed_raw, tmp_path):
    dest = tmp_path / "slice" / "dailymed"
    gate_slice.SLICES["dailymed"].build(dailymed_raw, dest)
    found = discover_outer_zips(dest, KINDS)
    assert sorted(p.name for p in found) == [
        "dm_spl_release_human_otc_part1.zip",
        "dm_spl_release_human_rx_part1.zip",
    ]
    assert release_part_gaps(found) == []


def test_dailymed_slice_without_a_release_is_refused(tmp_path):
    raw = tmp_path / "raw" / "dailymed"
    raw.mkdir(parents=True)
    with pytest.raises(gate_slice.SliceError, match="dailymed"):
        gate_slice.SLICES["dailymed"].build(raw, tmp_path / "slice")


def test_dailymed_slice_skips_a_corrupt_xml_candidate_and_counts_it(tmp_path):
    raw = tmp_path / "raw" / "dailymed"
    _outer(
        raw / "dm_spl_release_human_rx_part1.zip",
        {
            "prescription/20240101_a1.zip": _inner(
                b"<document><not closed",
            ),
            "prescription/20240101_a2.zip": _inner(_xml(_sid(2), padding=2_000, products=3)),
        },
    )
    _outer(
        raw / "dm_spl_release_human_otc_part1.zip", {"otc/20240101_c1.zip": _inner(_xml(_sid(5)))}
    )
    meta = gate_slice.SLICES["dailymed"].build(raw, tmp_path / "slice")
    assert meta["skipped_candidates"] == 1
    assert meta["labels"]["prescription"] == ["prescription/20240101_a2.zip"]


def test_dailymed_slice_with_only_corrupt_candidates_is_refused(tmp_path):
    raw = tmp_path / "raw" / "dailymed"
    _outer(
        raw / "dm_spl_release_human_rx_part1.zip",
        {"prescription/20240101_a1.zip": _inner(b"<document><not closed")},
    )
    _outer(
        raw / "dm_spl_release_human_otc_part1.zip", {"otc/20240101_c1.zip": _inner(_xml(_sid(5)))}
    )
    with pytest.raises(gate_slice.SliceError, match="no readable prescription label"):
        gate_slice.SLICES["dailymed"].build(raw, tmp_path / "slice")


def test_dailymed_slice_picks_the_same_labels_every_time(dailymed_raw, tmp_path):
    a = gate_slice.SLICES["dailymed"].build(dailymed_raw, tmp_path / "a")
    b = gate_slice.SLICES["dailymed"].build(dailymed_raw, tmp_path / "b")
    assert a == b


# ---------------------------------------------------------------------------
# EPAR
# ---------------------------------------------------------------------------
def _doc(doc_id: int, typ: str = "product-information") -> dict:
    return {
        "id": str(doc_id),
        "type": typ,
        "document_url": f"{PREFIX}en/documents/{typ}/doc-{doc_id}_en.pdf",
        "last_updated_date": "2024-01-01",
        "name": f"Medicine {doc_id}",
    }


def _epar_raw(tmp_path, sizes: dict[int, int | None]) -> Path:
    """A feed of one document per key; a size of None is a listed document with no file."""
    raw = tmp_path / "raw" / "ema"
    rows = [_doc(i) for i in sizes] + [_doc(900, "other-document")]
    raw.mkdir(parents=True)
    (raw / "ema_epar_documents.json").write_text(
        json.dumps({"meta": {"total_records": len(rows)}, "data": rows}), encoding="utf-8"
    )
    for i, size in sizes.items():
        if size is not None:
            _w(raw / "epar" / f"{i}.pdf", size)
    return raw


def test_epar_slice_cuts_the_feed_to_the_largest_pdfs(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_slice, "EPAR_LARGEST", 2)
    monkeypatch.setattr(gate_slice, "_pdf_pages", lambda p: None)  # no scanned-looking file
    raw = _epar_raw(tmp_path, {1: 100, 2: 5000, 3: 300, 4: 9000, 5: None})
    dest = tmp_path / "slice" / "ema"
    meta = gate_slice.SLICES["ema_epar"].build(raw, dest)
    assert sorted(meta["files"]) == ["ema_epar_documents.json", "epar/2.pdf", "epar/4.pdf"]
    assert sorted(meta["inputs"]) == ["epar/2.pdf", "epar/4.pdf"]  # the units: the PDFs
    feed = json.loads((dest / "ema_epar_documents.json").read_text(encoding="utf-8"))
    # Only the chosen documents are listed, so none counts as "not downloaded".
    assert sorted(d["id"] for d in feed["data"]) == ["2", "4"]
    assert feed["meta"]["total_records"] == 2
    assert (dest / "epar" / "4.pdf").stat().st_size == 9000


def test_epar_slice_adds_one_scanned_looking_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(gate_slice, "EPAR_LARGEST", 1)
    # Pages: file 2 has 1 page for 400,000 bytes (a scan); the others have text pages.
    pages = {"1.pdf": 500, "2.pdf": 1, "3.pdf": 400, "4.pdf": 50}
    monkeypatch.setattr(gate_slice, "_pdf_pages", lambda p: pages[p.name])
    raw = _epar_raw(tmp_path, {1: 900_000, 2: 400_000, 3: 300_000, 4: 200_000})
    meta = gate_slice.SLICES["ema_epar"].build(raw, tmp_path / "slice" / "ema")
    assert sorted(meta["files"]) == ["ema_epar_documents.json", "epar/1.pdf", "epar/2.pdf"]


def test_epar_slice_without_a_pdf_is_refused(tmp_path):
    raw = _epar_raw(tmp_path, {1: None})
    with pytest.raises(gate_slice.SliceError, match="ema_epar"):
        gate_slice.SLICES["ema_epar"].build(raw, tmp_path / "slice" / "ema")


# ---------------------------------------------------------------------------
# AACT: the whole file is the sample
# ---------------------------------------------------------------------------
def _aact_raw(tmp_path) -> Path:
    raw = tmp_path / "raw" / "aact"
    _w(raw / "20261003_export_ctgov.zip", 40)  # an older release
    _w(raw / "20261103_export_ctgov.zip", 50)  # the newest release
    _w(raw / "postgres_data.zip", 60)  # not a release zip
    _w(raw / "aact_data_dictionary_2026-11-03.csv", 5)
    return raw


def test_aact_slice_is_the_whole_file(tmp_path):
    _aact_raw(tmp_path)
    got = gate_slice.select("aact", tmp_path / "raw")
    assert [p.name for p in got] == ["20261103_export_ctgov.zip"]
    processed = tmp_path / "proc"
    sample = processed / "_sample" / "aact"
    meta = gate_slice.prepare("aact", tmp_path / "raw", sample, processed_root=processed)
    assert meta["files"] == ["20261103_export_ctgov.zip"]
    assert meta["inputs"] == ["20261103_export_ctgov.zip"]
    assert (sample / "01_raw" / "aact" / "20261103_export_ctgov.zip").stat().st_size == 50
    assert gate_slice.SLICES["aact"].raw_subpath == Path("aact")
    assert gate_slice.SLICES["aact"].max_bytes >= 3 * gate_slice.GB  # the zip is 2.4 GiB


def test_aact_slice_without_a_release_zip_is_refused(tmp_path):
    _w(tmp_path / "raw" / "aact" / "postgres_data.zip", 10)
    with pytest.raises(gate_slice.SliceError, match="aact"):
        gate_slice.select("aact", tmp_path / "raw")


def test_aact_slice_with_two_zips_of_one_release_is_refused(tmp_path):
    raw = tmp_path / "raw" / "aact"
    _w(raw / "20261103_export_ctgov.zip", 10)
    _w(raw / "old" / "20261103_export_ctgov.zip", 10)
    with pytest.raises(gate_slice.SliceError, match="20261103"):
        gate_slice.select("aact", tmp_path / "raw")


# ---------------------------------------------------------------------------
# The sample path of each stage skips the whole-issue checks
# ---------------------------------------------------------------------------
def test_the_processed_root_run_pipeline_builds_counts_as_a_sample(tmp_path):
    """run_pipeline.sh sets EPISTEME_PROCESSED_ROOT to <pass root>/_sample/<source>/02_processed.
    DailyMed and EPAR guards read that through ``bucket_parts.is_sample``."""
    for source in ("dailymed", "ema_epar"):
        processed = tmp_path / "pass_root" / "_sample" / source / "02_processed"
        assert bucket_parts.is_sample(processed)
    assert not bucket_parts.is_sample(tmp_path / "pass_root")
