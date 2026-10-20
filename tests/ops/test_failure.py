"""Failure handoff: a stage records the failed unit; the runner reads it back."""

from __future__ import annotations

import json

import pytest

from episteme.config import get_settings
from episteme.ops import failure


@pytest.fixture(autouse=True)
def _fresh_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _root(monkeypatch, tmp_path):
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    monkeypatch.setenv("EPISTEME_RUN_ID", "src-1")
    get_settings.cache_clear()


def test_record_and_show_round_trip(monkeypatch, tmp_path, capsys):
    _root(monkeypatch, tmp_path)
    path = failure.record_failure("src", "load", "file_7.xml", "x" * 900)
    assert path == tmp_path / "_ops" / "src" / "last_failure.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["run_id"] == "src-1"
    assert data["stage"] == "load"
    assert data["unit"] == "file_7.xml"
    assert len(data["error"]) == 500
    assert data["at"]
    assert failure.main(["show", "--source", "src", "--run-id", "src-1"]) == 0
    assert capsys.readouterr().out.strip() == "file_7.xml: " + "x" * 500


def test_show_prints_nothing_on_run_id_mismatch_or_missing(monkeypatch, tmp_path, capsys):
    _root(monkeypatch, tmp_path)
    assert failure.main(["show", "--source", "src", "--run-id", "src-1"]) == 0
    failure.record_failure("src", "load", "u", "e", run_id="old")
    assert failure.main(["show", "--source", "src", "--run-id", "src-1"]) == 0
    assert capsys.readouterr().out == ""


def test_show_field_unit_prints_the_unit_alone(monkeypatch, tmp_path, capsys):
    _root(monkeypatch, tmp_path)
    failure.record_failure("src", "load", "a: b.xml", "boom: x")
    assert failure.main(["show", "--source", "src", "--run-id", "src-1", "--field", "unit"]) == 0
    assert capsys.readouterr().out == "a: b.xml\n"
    failure.record_failure("src", "load", "", "boom")
    assert failure.main(["show", "--source", "src", "--run-id", "src-1", "--field", "unit"]) == 0
    assert capsys.readouterr().out == ""


def test_clear_removes_the_record_and_tolerates_absence(monkeypatch, tmp_path):
    _root(monkeypatch, tmp_path)
    path = failure.record_failure("src", "load", "u", "e")
    failure.clear("src")
    assert not path.exists()
    assert failure.main(["clear", "--source", "src"]) == 0
