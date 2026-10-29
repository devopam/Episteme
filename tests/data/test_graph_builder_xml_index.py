"""Non-pg tests for the graph phase's one-walk XML index."""

import logging
import os
from pathlib import Path

from episteme.data import graph_builder


def _touch(root: Path, rel: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("<a/>", encoding="utf-8")
    return p


def test_xml_index_keys_by_version_id(tmp_path):
    v1 = _touch(tmp_path, "xml/PMC1043859.1.xml")
    v3 = _touch(tmp_path, "xml/deep/PMC1043859.3.xml")
    v2 = _touch(tmp_path, "xml/PMC1043859.2.xml")
    bare = _touch(tmp_path, "PMC7.xml")
    other = _touch(tmp_path, "xml/PMC10438590.1.xml")
    _touch(tmp_path, "xml/PMC5.1.txt")

    index = graph_builder._xml_index(tmp_path)

    assert index["PMC1043859.1"] == v1
    assert index["PMC1043859.2"] == v2
    assert index["PMC1043859.3"] == v3
    assert index["PMC7"] == bare
    assert index["PMC10438590.1"] == other
    assert "PMC5.1" not in index
    # exact match only: a bare id or a prefix never matches
    assert graph_builder._find_xml(index, "PMC1043859") is None
    assert graph_builder._find_xml(index, "PMC1.1") is None
    assert graph_builder._find_xml(index, "PMC1043859.2") == v2
    assert graph_builder._find_xml(index, None) is None
    assert graph_builder._find_xml(index, "") is None


def test_row_version_id_picks_the_lower_version_on_disk(tmp_path):
    _touch(tmp_path, "xml/PMC9.2.xml")
    lower = _touch(tmp_path, "xml/PMC9.1.xml")

    index = graph_builder._xml_index(tmp_path)

    # the loaded row says source_record_id == "PMC9.1": the newer file is ignored
    assert graph_builder._find_xml(index, "PMC9.1") == lower


def test_same_stem_prefers_shared_xml_then_smallest_path(tmp_path):
    _touch(tmp_path, "batch_b/xml/PMC4.1.xml")
    _touch(tmp_path, "batch_a/xml/PMC4.1.xml")
    first = _touch(tmp_path, "batch_a/PMC4.1.xml")
    assert graph_builder._xml_index(tmp_path)["PMC4.1"] == first

    shared = _touch(tmp_path, "xml/PMC4.1.xml")
    assert graph_builder._xml_index(tmp_path)["PMC4.1"] == shared


def test_unreadable_root_logs_an_error(tmp_path, caplog):
    missing = tmp_path / "nope"
    with caplog.at_level(logging.WARNING, logger=graph_builder.__name__):
        index = graph_builder._xml_index(missing)

    assert index == {}
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and str(missing) in errors[0].getMessage()


def test_unreadable_subdirectory_logs_a_warning_and_continues(tmp_path, monkeypatch, caplog):
    good = _touch(tmp_path, "ok/PMC1.1.xml")
    (tmp_path / "bad").mkdir()
    real = os.scandir

    def flaky(path="."):
        if str(path).endswith("bad"):
            raise PermissionError("denied")
        return real(path)

    monkeypatch.setattr(os, "scandir", flaky)
    with caplog.at_level(logging.WARNING, logger=graph_builder.__name__):
        index = graph_builder._xml_index(tmp_path)

    assert index == {"PMC1.1": good}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "bad" in warnings[0].getMessage()
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


def test_version_rank_orders_components_numerically():
    rank = graph_builder._version_rank
    assert rank("2") < rank("2.1") < rank("2.10") < rank("3")
    assert rank("9") < rank("10")
    assert rank("x") < rank("0")  # non-numeric ranks lowest
    assert rank("a") < rank("b")  # and deterministically
    assert rank("2") != rank("2.1")


def test_find_xml_uses_the_index_once(tmp_path, monkeypatch):
    _touch(tmp_path, "a/PMC1.1.xml")
    _touch(tmp_path, "b/PMC2.1.xml")
    _touch(tmp_path, "b/c/PMC3.1.xml")

    walks = {"n": 0}
    real = os.scandir

    def counting(path="."):
        walks["n"] += 1
        return real(path)

    monkeypatch.setattr(os, "scandir", counting)
    index = graph_builder._xml_index(tmp_path)
    after_build = walks["n"]
    assert after_build >= 1

    for _ in range(50):
        for pmcid in ("PMC1", "PMC2", "PMC3", "PMC9"):
            graph_builder._find_xml(index, pmcid)

    assert walks["n"] == after_build  # lookups never touch the disk
