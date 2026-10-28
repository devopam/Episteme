"""Non-pg tests for the graph phase's one-walk XML index."""

import os
from pathlib import Path

from episteme.data import graph_builder


def _touch(root: Path, rel: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("<a/>", encoding="utf-8")
    return p


def test_xml_index_picks_the_highest_version(tmp_path):
    _touch(tmp_path, "xml/PMC1043859.1.xml")
    v3 = _touch(tmp_path, "xml/deep/PMC1043859.3.xml")
    _touch(tmp_path, "xml/PMC1043859.2.xml")
    bare = _touch(tmp_path, "PMC7.xml")
    other = _touch(tmp_path, "xml/PMC10438590.1.xml")
    _touch(tmp_path, "xml/PMC5.1.txt")

    index = graph_builder._xml_index(tmp_path)

    assert index["PMC1043859"] == v3
    assert index["PMC7"] == bare
    assert index["PMC10438590"] == other
    assert "PMC5" not in index
    # the match rule: PMC1 must not match PMC1043859 or PMC10438590
    assert graph_builder._find_xml(index, "PMC1") is None
    assert graph_builder._find_xml(index, "PMC1043859") == v3
    assert graph_builder._find_xml(index, None) is None
    assert graph_builder._find_xml(index, "") is None


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
