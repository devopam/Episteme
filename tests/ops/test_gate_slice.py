import inspect
import json
import os
import re
from pathlib import Path

import pytest

from episteme.ops import gate_slice


def _w(path: Path, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


@pytest.fixture
def roots(tmp_path):
    processed = tmp_path / "02_processed"
    return tmp_path / "01_raw", processed, processed / "_sample"


def _bookshelf(raw: Path, n: int = 60) -> Path:
    d = raw / "bookshelf"
    for i in range(n):
        _w(d / "packages" / f"b{i % 3}" / f"book{i:03d}.tar.gz", 100 + i)
    return d


def test_bookshelf_spread_deterministic_and_percentile(roots):
    raw, _, _ = roots
    _bookshelf(raw)
    a = gate_slice.select("bookshelf", raw)
    assert a == gate_slice.select("bookshelf", raw)
    names = [p.name for p in a]
    # 20 spread over 60 files; the 99th-percentile file (the largest) is on the spread
    assert len(a) == 20
    assert names[0] == "book000.tar.gz" and names[-1] == "book059.tar.gz"


def test_bookshelf_percentile_pick_added_when_not_in_spread(roots):
    raw, _, _ = roots
    d = raw / "bookshelf"
    for i in range(40):
        _w(d / f"book{i:03d}.tar.gz", 10 if i != 7 else 500)  # 7 is not on the spread
    names = [p.name for p in gate_slice.select("bookshelf", raw)]
    assert "book007.tar.gz" in names and len(names) == 21


def test_bookshelf_excludes_oversize(roots, monkeypatch):
    raw, _, _ = roots
    monkeypatch.setattr(gate_slice, "BOOKSHELF_MAX_PACKAGE_BYTES", 150)
    d = _bookshelf(raw, 10)  # sizes 100..109 all fit
    _w(d / "huge.tar.gz", 1000)
    names = [p.name for p in gate_slice.select("bookshelf", raw)]
    assert "huge.tar.gz" not in names and len(names) == 10


def test_relative_paths_kept_and_hard_link(roots):
    raw, processed, sample = roots
    _bookshelf(raw, 5)
    res = gate_slice.prepare("bookshelf", raw, sample / "bookshelf", processed_root=processed)
    dst = sample / "bookshelf" / "01_raw" / "bookshelf" / "packages" / "b0" / "book000.tar.gz"
    assert dst.is_file()
    assert "packages/b0/book000.tar.gz" in res["files"]
    assert os.stat(dst).st_nlink > 1
    assert res["linked"] == 5 and res["copied"] == 0
    meta = json.loads((sample / "bookshelf" / "slice.json").read_text())
    assert meta["files"] == res["files"] and meta["bytes"] == res["bytes"]


def test_copy_fallback(roots, monkeypatch):
    raw, processed, sample = roots
    _bookshelf(raw, 3)

    def boom(*a, **k):
        raise OSError("cross-device")

    monkeypatch.setattr(os, "link", boom)
    res = gate_slice.prepare("bookshelf", raw, sample / "bookshelf", processed_root=processed)
    assert res["copied"] == 3 and res["linked"] == 0


def test_prepare_wipes_old_slice(roots):
    raw, processed, sample = roots
    _bookshelf(raw, 3)
    stale = _w(sample / "bookshelf" / "01_raw" / "bookshelf" / "old.tar.gz")
    gate_slice.prepare("bookshelf", raw, sample / "bookshelf", processed_root=processed)
    assert not stale.exists()


def test_refuses_outside_sample_and_deletes_nothing(roots):
    raw, processed, sample = roots
    _bookshelf(raw, 3)
    for bad in (processed / "bookshelf", processed, sample, processed / "_sampler" / "x"):
        keep = _w(bad / "01_raw" / "bookshelf" / "keep.txt")
        with pytest.raises(gate_slice.SliceError, match="not inside"):
            gate_slice.prepare("bookshelf", raw, bad, processed_root=processed)
        assert keep.exists()


def test_cap_raises(roots, monkeypatch):
    raw, processed, sample = roots
    _bookshelf(raw, 3)
    monkeypatch.setitem(
        gate_slice.SLICES,
        "bookshelf",
        gate_slice.SliceSpec("bookshelf", 50, gate_slice._select_bookshelf, Path("bookshelf")),
    )
    with pytest.raises(gate_slice.SliceError, match="cap"):
        gate_slice.prepare("bookshelf", raw, sample / "bookshelf", processed_root=processed)


def test_missing_selected_file_errors(roots, monkeypatch):
    raw, processed, sample = roots
    d = _bookshelf(raw, 3)
    ghost = d / "ghost.tar.gz"
    monkeypatch.setattr(gate_slice, "select", lambda s, r: [ghost])
    with pytest.raises(FileNotFoundError, match="ghost"):
        gate_slice.prepare("bookshelf", raw, sample / "bookshelf", processed_root=processed)


def _europepmc(raw: Path) -> Path:
    d = raw / "europepmc" / "manuscripts"
    p = "author_manuscript_xml."
    for name in (
        p + "PMC001xxxxxx.baseline.2024-01-01.tar.gz",
        p + "PMC002xxxxxx.baseline.2024-01-01.tar.gz",
        p + "PMC003xxxxxx.baseline.2025-12-16.tar.gz",
        p + "incr.2026-03-01.tar.gz",
        p + "incr.2026-04-09.tar.gz",
    ):
        _w(d / name)
        _w(d / (name[: -len(".tar.gz")] + ".filelist.csv"))
        _w(d / (name[: -len(".tar.gz")] + ".filelist.txt"))
    return d


def test_europepmc_selection_with_siblings(roots):
    raw, processed, sample = roots
    _europepmc(raw)
    names = [p.name for p in gate_slice.select("europepmc_manuscript", raw)]
    pkgs = [n for n in names if n.endswith(".tar.gz")]
    assert sorted(pkgs) == sorted(
        [
            "author_manuscript_xml.PMC001xxxxxx.baseline.2024-01-01.tar.gz",
            "author_manuscript_xml.PMC003xxxxxx.baseline.2025-12-16.tar.gz",
            "author_manuscript_xml.incr.2026-04-09.tar.gz",
        ]
    )
    assert len(names) == 9
    res = gate_slice.prepare("europepmc_manuscript", raw, sample / "e", processed_root=processed)
    assert len(res["files"]) == 9
    # F2: the units of work (what discover() returns) are listed apart from the
    # .filelist sidecars, so the yield is per archive.
    assert res["inputs"] == [f for f in res["files"] if f.endswith(".tar.gz")]
    assert len(res["inputs"]) == 3
    meta = json.loads((sample / "e" / "slice.json").read_text())
    assert meta["inputs"] == res["inputs"] and meta["files"] == res["files"]


def test_bookshelf_inputs_are_all_slice_files(roots):
    raw, processed, sample = roots
    _bookshelf(raw, 5)
    res = gate_slice.prepare("bookshelf", raw, sample / "bookshelf", processed_root=processed)
    assert res["inputs"] == res["files"]


def test_europepmc_missing_incremental_raises(roots):
    raw, _, _ = roots
    d = raw / "europepmc" / "manuscripts"
    _w(d / "author_manuscript_xml.PMC001xxxxxx.baseline.2024-01-01.tar.gz")
    with pytest.raises(gate_slice.SliceError, match="incremental"):
        gate_slice.select("europepmc_manuscript", raw)


APOLLO = {
    "train/pretrain/medicalBook_en_qa.json": 900,
    "train/pretrain/medicalWiki_fr_qa.json": 130,
    "train/pretrain/medicalWiki_hi_qa.json": 4,
    "train/pretrain/medicalWiki_fr_text.json": 157,
    "train/pretrain/medicalBook_en_text.json": 1300,
    "train/sft/code_en.json": 105,
    "train/sft/medicalExam_en_clean.json": 190,
    "train/sft/medicalExam_es.json": 20,
    "train/sft/medicalExam_en_dup.json": 0,
    "check_exam/questions/en_question.json": 500,
    "README.md": 1,
}


def _apollo(raw: Path, sizes: dict[str, int]) -> Path:
    d = raw / "apollo"
    for rel, size in sizes.items():
        _w(d / rel, size)
    return d


def test_apollo_smallest_over_threshold_per_group(roots, monkeypatch):
    raw, processed, sample = roots
    monkeypatch.setattr(gate_slice, "APOLLO_MIN_BYTES", 100)
    d = _apollo(raw, APOLLO)
    rels = [p.relative_to(d).as_posix() for p in gate_slice.select("apollo", raw)]
    assert rels == [
        "train/pretrain/medicalWiki_fr_qa.json",
        "train/pretrain/medicalWiki_fr_text.json",
        "train/sft/medicalExam_en_clean.json",
    ]
    res = gate_slice.prepare("apollo", raw, sample / "apollo", processed_root=processed)
    assert res["files"] == rels


def test_apollo_group_without_qualifying_file_raises(roots, monkeypatch):
    raw, _, _ = roots
    monkeypatch.setattr(gate_slice, "APOLLO_MIN_BYTES", 100)
    sizes = {k: v for k, v in APOLLO.items() if "sft/medicalExam_en_clean" not in k}
    _apollo(raw, sizes)
    with pytest.raises(gate_slice.SliceError, match="train/sft medical"):
        gate_slice.select("apollo", raw)


def test_unknown_source(roots):
    with pytest.raises(gate_slice.SliceError):
        gate_slice.select("nope", roots[0])


def test_slice_lands_at_raw_subpath_per_source(roots, monkeypatch):
    raw, processed, sample = roots
    monkeypatch.setattr(gate_slice, "APOLLO_MIN_BYTES", 100)
    _bookshelf(raw, 3)
    _europepmc(raw)
    _apollo(raw, APOLLO)
    for source in ("bookshelf", "europepmc_manuscript", "apollo"):
        sub = gate_slice.SLICES[source].raw_subpath
        res = gate_slice.prepare(source, raw, sample / source, processed_root=processed)
        assert res["files"]
        for rel in res["files"]:
            assert (sample / source / "01_raw" / sub / rel).is_file()


def test_raw_subpath_matches_extractor_default():
    from episteme.data.apollo import extract_apollo
    from episteme.data.bookshelf import extract_bookshelf
    from episteme.data.europepmc.manuscripts import extract_europepmc_manuscripts as em

    mods = {
        "bookshelf": extract_bookshelf,
        "apollo": extract_apollo,
        "europepmc_manuscript": em,
    }
    for source, mod in mods.items():
        src = inspect.getsource(mod)
        m = re.search(r'settings\.raw_root((?:\s*/\s*"\w+")+)', src)
        assert m, source
        parts = re.findall(r'"(\w+)"', m.group(1))
        assert Path(*parts) == gate_slice.SLICES[source].raw_subpath


def test_failed_prepare_leaves_no_stale_slice_json(roots, monkeypatch):
    raw, processed, sample = roots
    _bookshelf(raw, 3)
    gate_slice.prepare("bookshelf", raw, sample / "b", processed_root=processed)
    assert (sample / "b" / "slice.json").is_file()

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(os, "link", boom)
    monkeypatch.setattr(gate_slice.shutil, "copy2", boom)
    with pytest.raises(RuntimeError):
        gate_slice.prepare("bookshelf", raw, sample / "b", processed_root=processed)
    assert not (sample / "b" / "slice.json").exists()


def test_refuses_symlinked_dest(roots, tmp_path):
    raw, processed, sample = roots
    _bookshelf(raw, 3)
    real = tmp_path / "elsewhere"
    real.mkdir()
    (sample / "b" / "01_raw").mkdir(parents=True)
    try:
        os.symlink(real, sample / "b" / "01_raw" / "bookshelf", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not permitted")
    with pytest.raises(gate_slice.SliceError, match="symlink"):
        gate_slice.prepare("bookshelf", raw, sample / "b", processed_root=processed)
