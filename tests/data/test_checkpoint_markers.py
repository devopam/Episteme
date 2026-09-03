import json

from episteme.data.checkpoint_markers import (
    failed_marker_path,
    graph_success_marker_path,
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
