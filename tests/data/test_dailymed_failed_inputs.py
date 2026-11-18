"""DailyMed ``failed_inputs.json``: the count for the gate, and the file name and error of
each failure, so a later discard decision can name them (spec section 8)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from episteme.data.dailymed import extract_dailymed as dm

from ._dailymed_fixtures import inner_zip, outer_zip, set_id, spl_xml

KINDS = ("prescription", "otc")
OUTER = "dm_spl_release_human_rx_part1.zip"
S_AB, S_AB2 = set_id("ab", 1), set_id("ab", 2)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    from episteme.config import get_settings

    monkeypatch.setenv("EPISTEME_RUN_ID", "run-1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _name(sid: str) -> str:
    return f"prescription/20240101_{sid}.zip"


def _listed(out: Path) -> dict:
    return json.loads((out / "_ops" / "dailymed" / "failed_inputs.json").read_text("utf-8"))


def test_failed_inputs_lists_files(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    no_xml = io.BytesIO()
    with zipfile.ZipFile(no_xml, "w") as zf:
        zf.writestr("figure.jpg", b"img")
    outer_zip(
        raw / OUTER,
        {
            _name(S_AB): inner_zip(spl_xml(S_AB)),
            _name("corrupt-1"): b"this is not a zip file",
            _name("noxml-1"): no_xml.getvalue(),
        },
    )
    res = dm.extract_dailymed(raw, out, release_kinds=KINDS)
    dm._write_failed_inputs(out, res)
    listed = _listed(out)
    # The count is unchanged; each failure is named by its input key (relative to the
    # raw dir, never an absolute path) with its error.
    assert (listed["run_id"], listed["failed_inputs"]) == ("run-1", 2)
    files = listed["failed_files"]
    assert [f["file"] for f in files] == [
        f"{OUTER}__prescription__20240101_corrupt-1.zip",
        f"{OUTER}__prescription__20240101_noxml-1.zip",
    ]
    assert all(f["error"] for f in files)
    assert str(tmp_path) not in json.dumps(listed)


def test_a_held_bucket_is_listed_with_its_label(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    good = {_name(S_AB): inner_zip(spl_xml(S_AB)), _name(S_AB2): inner_zip(spl_xml(S_AB2))}
    outer_zip(raw / OUTER, good)
    dm.extract_dailymed(raw, out, release_kinds=KINDS)
    # The next release holds a corrupt copy of S_AB: the label fails and its bucket is held.
    outer_zip(raw / OUTER, {**good, _name(S_AB): b"damaged in the download"})
    res = dm.extract_dailymed(raw, out, release_kinds=KINDS)
    dm._write_failed_inputs(out, res)
    listed = _listed(out)
    assert listed["failed_inputs"] == 2
    files = {f["file"]: f["error"] for f in listed["failed_files"]}
    assert set(files) == {f"{OUTER}__prescription__20240101_{S_AB}.zip", "dailymed_bab"}
    assert files["dailymed_bab"].startswith("bucket not rewritten")


def test_no_failure_lists_no_file(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    outer_zip(raw / OUTER, {_name(S_AB): inner_zip(spl_xml(S_AB))})
    res = dm.extract_dailymed(raw, out, release_kinds=KINDS)
    dm._write_failed_inputs(out, res)
    assert _listed(out)["failed_inputs"] == 0
    assert _listed(out)["failed_files"] == []
