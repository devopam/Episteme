import json

from episteme.sft.manifest import write_manifest


def test_manifest_hash_is_stable_for_equal_config(tmp_path):
    cfg_a = {"seed": 1, "weights": {"a": 1.0, "b": 2.0}}
    cfg_b = {"weights": {"b": 2.0, "a": 1.0}, "seed": 1}
    h1 = write_manifest(tmp_path / "m1.json", "slice", inputs=[], counts={"x": 1}, config=cfg_a)
    h2 = write_manifest(
        tmp_path / "m2.json", "batch", inputs=[{"p": 1}], counts={"x": 2}, config=cfg_b
    )
    h3 = write_manifest(tmp_path / "m3.json", "slice", inputs=[], counts={}, config={"seed": 2})
    assert h1 == h2
    assert h1 != h3
    doc = json.loads((tmp_path / "m1.json").read_text(encoding="utf-8"))
    assert doc["config_hash"] == h1
    assert doc["kind"] == "slice"
    assert doc["created_at"]
    assert doc["teacher"] is None
    assert doc["budget_used"] is None


def test_manifest_records_teacher_and_budget(tmp_path):
    write_manifest(
        tmp_path / "m.json", "round", inputs=[], counts={}, config={}, teacher="t", budget_used=7
    )
    doc = json.loads((tmp_path / "m.json").read_text(encoding="utf-8"))
    assert doc["teacher"] == "t"
    assert doc["budget_used"] == 7


def test_manifest_extra_fields_do_not_change_the_hash(tmp_path):
    h1 = write_manifest(tmp_path / "a.json", "batch", inputs=[], counts={}, config={"k": 1})
    h2 = write_manifest(
        tmp_path / "b.json",
        "batch",
        inputs=[],
        counts={},
        config={"k": 1},
        extra={"stopped": "budget"},
    )
    assert h1 == h2
    doc = json.loads((tmp_path / "b.json").read_text(encoding="utf-8"))
    assert doc["stopped"] == "budget"
    import pytest

    with pytest.raises(ValueError):
        write_manifest(
            tmp_path / "c.json", "batch", inputs=[], counts={}, config={}, extra={"kind": 1}
        )
