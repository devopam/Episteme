"""The pmc sample slice: three buckets chosen by XML bytes and version count."""

from __future__ import annotations

from pathlib import Path

import pytest

from episteme.ops import gate, gate_slice

BUCKET = 50_000


def _w(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _article(raw: Path, pmc_id: int, version: int, xml_bytes: int) -> tuple[Path, Path]:
    stem = f"PMC{pmc_id}.{version}"
    meta = _w(raw / "metadata" / f"{stem}.json", 5)
    xml = _w(raw / "xml" / f"{stem}.xml", xml_bytes)
    return meta, xml


@pytest.fixture
def raw(tmp_path) -> Path:
    return tmp_path / "01_raw" / "pmc" / "oa_comm"


def _five_buckets(raw: Path) -> dict[int, list[Path]]:
    """Buckets 1..5. XML bytes: 1=30, 2=500 (largest), 3=200 (median), 4=100,
    5=60. Versions superseded: 4 has three, the others at most one."""
    files: dict[int, list[Path]] = {}
    spec = {
        1: [(1, 1, 30)],
        2: [(1, 1, 500)],
        3: [(1, 1, 200)],
        4: [(1, 1, 50), (1, 2, 25), (1, 3, 25), (2, 1, 0), (2, 2, 0)],
        5: [(1, 1, 30), (1, 2, 30)],
    }
    for b, items in spec.items():
        files[b] = []
        for n, v, size in items:
            meta, xml = _article(raw, b * BUCKET + n, v, size)
            files[b] += [meta, xml]
    return files


def test_select_pmc_picks_largest_median_and_most_versions(raw):
    files = _five_buckets(raw)
    got = gate_slice._select_pmc(raw)
    # Kept XML bytes: b1=30, b2=500, b3=200, b4=25, b5=30. Largest = 2. Sorted by
    # (bytes, bucket): 4, 1, 5, 3, 2 -> the median is bucket 5. Most superseded = 4.
    expected = {2, 5, 4}
    assert len(set(got)) == len(got)
    want = {p for b in expected for p in files[b]}
    assert set(got) == want
    assert gate_slice._select_pmc(raw) == got  # deterministic


def test_select_pmc_one_bucket_chosen_twice_is_listed_once(raw):
    # One bucket is the largest and has the most superseded versions.
    _article(raw, 1 * BUCKET + 1, 1, 10)
    _article(raw, 1 * BUCKET + 1, 2, 900)
    got = gate_slice._select_pmc(raw)
    assert len(got) == len(set(got)) == 4  # two metadata and two XML files


def test_select_pmc_ties_take_the_lowest_bucket(raw):
    for b in (7, 3, 5):
        _article(raw, b * BUCKET + 1, 1, 100)
    got = gate_slice._select_pmc(raw)
    names = sorted(p.name for p in got)
    # largest tie -> bucket 3; median -> bucket 5; most superseded (all 0) -> bucket 3.
    assert names == sorted(
        [f"PMC{3 * BUCKET + 1}.1.json", f"PMC{3 * BUCKET + 1}.1.xml"]
        + [f"PMC{5 * BUCKET + 1}.1.json", f"PMC{5 * BUCKET + 1}.1.xml"]
    )


def test_select_pmc_without_files_raises(raw):
    raw.mkdir(parents=True)
    with pytest.raises(gate_slice.SliceError):
        gate_slice._select_pmc(raw)


def test_pmc_slice_spec():
    spec = gate_slice.SLICES["pmc"]
    assert spec.max_bytes == 2 * gate_slice.GB
    assert spec.raw_subpath == Path("pmc/oa_comm")
    assert spec.is_unit(Path("PMC1.1.json"))
    assert not spec.is_unit(Path("PMC1.1.xml"))


def test_prepare_links_the_slice_under_the_raw_subpath(tmp_path, raw):
    _five_buckets(raw)
    processed = tmp_path / "02_processed"
    res = gate_slice.prepare(
        "pmc", tmp_path / "01_raw", processed / "_sample" / "pmc", processed_root=processed
    )
    assert res["inputs"] and all(r.endswith(".json") for r in res["inputs"])
    for rel in res["files"]:
        assert (processed / "_sample" / "pmc" / "01_raw" / "pmc" / "oa_comm" / rel).is_file()


def test_pmc_is_gated_for_extract_and_load():
    assert gate.is_gated("pmc", "extract")
    assert gate.is_gated("pmc", "load")
    rules = gate.load_rules("pmc")
    for stage in ("extract", "load"):
        assert not set(rules[stage]) - gate._KNOWN_KEYS
    assert gate.stage_module("pmc", "extract")[0] == "episteme.data.pmc.extract_pmc"
    assert rules["extract"]["allowed_licenses"] == ["commercial"]
    assert rules["extract"]["max_peak_rss_mb"] == 2048
    assert rules["extract"]["parts_within_caps"] is True
