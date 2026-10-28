"""PMC id buckets: boundaries, highest version, manifests, fingerprints."""

from __future__ import annotations

import json
from pathlib import Path

from episteme.data import checkpoint_markers
from episteme.data.pmc import buckets as b


def _touch(root: Path, rel: str, text: str = "{}") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_bucket_boundaries() -> None:
    assert b.BUCKET_IDS == 50_000
    assert b.bucket_of(49_999) == 0
    assert b.bucket_of(50_000) == 1
    assert b.bucket_key(20) == "pmc_b0020"
    assert b.part_name(20, 3) == "pmc_b0020__part00003"


def test_parse_meta_name() -> None:
    assert b.parse_meta_name("PMC1043859.2.json") == (1043859, 2)
    for bad in ("notes.json", "PMCx.1.json", "PMC10.json", "PMC10.1.xml"):
        assert b.parse_meta_name(bad) is None


def test_highest_version_wins(tmp_path: Path) -> None:
    files = [
        _touch(tmp_path, f"metadata/{n}")
        for n in ("PMC10.1.json", "PMC10.3.json", "PMC10.2.json", "PMC11.1.json")
    ]
    grouped, unrecognised = b.group_by_bucket(files, tmp_path)
    assert unrecognised == 0
    assert set(grouped) == {0}
    bucket = grouped[0]
    assert sorted(p.name for p in bucket.files) == ["PMC10.3.json", "PMC11.1.json"]
    assert bucket.superseded == 2


def test_same_version_in_two_folders_keeps_one(tmp_path: Path) -> None:
    a = _touch(tmp_path, "a/PMC10.1.json")
    c = _touch(tmp_path, "b/PMC10.1.json")
    grouped, _ = b.group_by_bucket([c, a], tmp_path)
    bucket = grouped[0]
    assert bucket.files == (a,)
    assert bucket.duplicate_files == 1
    assert bucket.superseded == 0


def test_unrecognised_names_are_counted_not_fatal(tmp_path: Path) -> None:
    files = [_touch(tmp_path, n) for n in ("PMC10.1.json", "notes.json", "PMCx.1.json")]
    grouped, unrecognised = b.group_by_bucket(files, tmp_path)
    assert list(grouped) == [0]
    assert unrecognised == 2


def test_fingerprint_changes_when_a_file_changes(tmp_path: Path) -> None:
    meta = _touch(tmp_path, "metadata/PMC10.1.json", "{}")
    _touch(tmp_path, "metadata/PMC10.1.xml", "<a/>")
    grouped, _ = b.group_by_bucket([meta], tmp_path)
    processed = tmp_path / "processed"
    path = b.write_manifest(processed, grouped[0], tmp_path)
    first = b.read_manifest(path)
    assert json.loads(json.dumps(first["fingerprint"])) == first["fingerprint"]
    assert len(first["fingerprint"]) == 2
    assert all(isinstance(e, list) for e in first["fingerprint"])
    assert b.bucket_fingerprint(grouped[0], tmp_path) == first["fingerprint"]

    meta.write_text('{"changed": "yes, longer"}', encoding="utf-8")
    assert b.bucket_fingerprint(grouped[0], tmp_path) != first["fingerprint"]


def test_manifest_content_and_location(tmp_path: Path) -> None:
    files = [
        _touch(tmp_path, "metadata/PMC10.1.json"),
        _touch(tmp_path, "metadata/PMC10.2.json"),
    ]
    grouped, _ = b.group_by_bucket(files, tmp_path)
    processed = tmp_path / "processed"
    path = b.write_manifest(processed, grouped[0], tmp_path)
    assert path == checkpoint_markers.ops_root(processed, "pmc") / "buckets" / "pmc_b0000.json"
    m = b.read_manifest(path)
    assert m["bucket"] == 0
    assert m["key"] == "pmc_b0000"
    assert m["files"] == ["metadata__PMC10.2.json"]
    assert m["superseded_versions"] == 1
    assert m["duplicate_files"] == 0


def test_bucket_names_do_not_collide_across_raw_roots(tmp_path: Path) -> None:
    root1, root2 = tmp_path / "r1", tmp_path / "r2"
    p1 = _touch(root1, "batch1/PMC10.1.json")
    p1b = _touch(root1, "batch2/PMC10.1.json")
    p2 = _touch(root2, "batch9/PMC10.1.json")
    g1, _ = b.group_by_bucket([p1, p1b], root1)
    g2, _ = b.group_by_bucket([p2], root2)
    # same basename in one root is not collapsed by basename
    assert g1[0].duplicate_files == 1
    assert g1[0].files == (p1,)
    proc1, proc2 = tmp_path / "p1", tmp_path / "p2"
    m1 = b.read_manifest(b.write_manifest(proc1, g1[0], root1))
    m2 = b.read_manifest(b.write_manifest(proc2, g2[0], root2))
    assert m1["key"] == m2["key"] == "pmc_b0000"
    assert m1["files"] == ["batch1__PMC10.1.json"]
    assert m2["files"] == ["batch9__PMC10.1.json"]
    assert (proc1 / "_ops" / "pmc" / "buckets" / "pmc_b0000.json").is_file()


def test_fingerprint_changes_when_an_xml_changes(tmp_path: Path) -> None:
    m1 = _touch(tmp_path, "metadata/PMC10.1.json", "{}")
    x1 = _touch(tmp_path, "metadata/PMC10.1.xml", "<a/>")
    m2 = _touch(tmp_path, "metadata/PMC11.1.json", "{}")
    grouped, _ = b.group_by_bucket([m1, m2], tmp_path)
    first = b.bucket_fingerprint(grouped[0], tmp_path)
    assert len(first) == 3  # two metadata files, one XML

    x1.write_text("<a>an XML-only change, longer</a>", encoding="utf-8")
    second = b.bucket_fingerprint(grouped[0], tmp_path)
    assert second != first

    _touch(tmp_path, "metadata/PMC11.1.xml", "<b/>")  # a new XML for PMC11
    third = b.bucket_fingerprint(grouped[0], tmp_path)
    assert third != second
    assert len(third) == 4


def test_fingerprint_does_not_search_the_xml_tree(tmp_path: Path, monkeypatch) -> None:
    meta = _touch(tmp_path, "metadata/PMC10.1.json", "{}")
    _touch(tmp_path, "xml/deep/PMC10.1.xml", "<a/>")  # only the rglob fallback finds it
    grouped, _ = b.group_by_bucket([meta], tmp_path)
    assert len(b.bucket_fingerprint(grouped[0], tmp_path)) == 1


def test_manifest_lists_paths_relative_to_the_raw_dir(tmp_path: Path) -> None:
    meta = _touch(tmp_path, "metadata/PMC10.1.json")
    grouped, _ = b.group_by_bucket([meta], tmp_path)
    m = b.read_manifest(b.write_manifest(tmp_path / "processed", grouped[0], tmp_path))
    assert m["paths"] == ["metadata/PMC10.1.json"]
    assert b.manifest_files(m, tmp_path) == [meta]
