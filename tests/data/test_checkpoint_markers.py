import json
from pathlib import Path

from episteme.data.checkpoint_markers import (
    discover_input_files,
    failed_marker_path,
    find_input_by_key,
    graph_success_marker_path,
    input_key,
    is_success,
    load_success_marker_path,
    mark_failed,
    mark_success,
    success_marker_path,
)


def test_mark_success_writes_marker_and_clears_failure(tmp_path):
    root = tmp_path / "02_processed"
    mark_failed(root, "pmc", "PMC1.1.xml", error_class="io_error", message="boom")
    assert failed_marker_path(root, "pmc", "PMC1.1.xml").is_file()
    mark_success(root, "pmc", "PMC1.1.xml", stats={"rows": 3})
    assert is_success(root, "pmc", "PMC1.1.xml")
    assert not failed_marker_path(root, "pmc", "PMC1.1.xml").is_file()
    payload = json.loads(success_marker_path(root, "pmc", "PMC1.1.xml").read_text())
    assert payload["stats"] == {"rows": 3}


def test_is_success_false_when_absent(tmp_path):
    assert not is_success(tmp_path / "02_processed", "pmc", "nope.xml")


def test_marker_path_layout(tmp_path):
    root = tmp_path / "02_processed"
    assert success_marker_path(root, "pmc", "f").parent.name == "success"
    assert load_success_marker_path(root, "pmc", "f").parent.name == "load_success"
    assert graph_success_marker_path(root, "pmc", "f").parent.name == "graph_success"
    assert load_success_marker_path(root, "pmc", "f").parts[-3] == "pmc"


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")
    return p


def test_flat_layout_key_is_the_bare_basename(tmp_path):
    f = _touch(tmp_path / "raw" / "desc2025.gz")
    assert input_key(f, tmp_path / "raw") == "desc2025.gz"


def test_nested_layout_key_is_relative_path_with_double_underscore(tmp_path):
    f = _touch(tmp_path / "raw" / "updated_date=2026-06-25" / "part_0000.gz")
    assert input_key(f, tmp_path / "raw") == "updated_date=2026-06-25__part_0000.gz"


def test_path_outside_root_falls_back_to_basename(tmp_path):
    f = _touch(tmp_path / "elsewhere" / "a.txt")
    assert input_key(f, tmp_path / "raw") == "a.txt"


def test_discover_keeps_same_basename_in_different_directories(tmp_path):
    raw = tmp_path / "raw"
    a = _touch(raw / "a" / "data.tsv")
    b = _touch(raw / "b" / "data.tsv")
    found = discover_input_files(raw, ["*.tsv"])
    assert found == [a, b]
    assert {input_key(p, raw) for p in found} == {"a__data.tsv", "b__data.tsv"}


def test_discover_does_not_duplicate_a_file_matched_by_glob_and_rglob(tmp_path):
    raw = tmp_path / "raw"
    _touch(raw / "one.tsv")
    assert len(discover_input_files(raw, ["*.tsv", "one.*"])) == 1


def test_find_input_by_key_roundtrip(tmp_path):
    raw = tmp_path / "raw"
    f = _touch(raw / "nlm" / "desc2026.gz")
    assert find_input_by_key(raw, input_key(f, raw)) == f
    assert find_input_by_key(raw, "missing.gz") is None


def test_find_input_by_key_handles_filenames_containing_double_underscore(tmp_path):
    raw = tmp_path / "raw"
    f = _touch(raw / "d" / "we__ird.txt")
    key = input_key(f, raw)
    assert key == "d__we__ird.txt"
    assert find_input_by_key(raw, key) == f
