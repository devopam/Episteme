"""The gate counts failed inputs beyond the exit code (pmc: failed articles)."""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

from episteme import config
from episteme.data.checkpoint_markers import FAILED_INPUTS_FILE, ops_root
from episteme.data.pmc import extract_pmc as pmc
from episteme.ops import gate


def _article(raw: Path, n: int, *, broken: bool = False) -> None:
    meta = raw / "metadata" / f"PMC{n}.1.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(
        "{not json"
        if broken
        else json.dumps(
            {"pmcid": f"PMC{n}", "version": 1, "title": f"T{n}", "license_code": "CC BY"}
        ),
        encoding="utf-8",
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    out = tmp_path / "02_processed"
    settings = dataclasses.replace(config.get_settings(), run_id="run-1", processed_root=out)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(pmc, "get_settings", lambda: settings)
    raw = tmp_path / "raw"
    raw.mkdir()
    return raw, out


def _extract(raw: Path, out: Path) -> int:
    return pmc._main(["--raw-dir", str(raw), "--processed-dir", str(out), "--workers", "1"])


def _gate_run(tmp_path: Path, source: str | None = "pmc") -> dict:
    rec = tmp_path / "rec.json"
    argv = ["run", "--record", str(rec)]
    if source:
        argv += ["--source", source, "--stage", "extract"]
    assert gate.main([*argv, "--", sys.executable, "-c", "pass"]) == 0
    return json.loads(rec.read_text(encoding="utf-8"))


def test_one_broken_article_fails_max_failed_inputs(tmp_path, env):
    raw, out = env
    _article(raw, 1)
    _article(raw, 2, broken=True)
    assert _extract(raw, out) == 0  # the bucket is fine: the exit code stays 0
    f = ops_root(out, "pmc") / FAILED_INPUTS_FILE
    assert json.loads(f.read_text(encoding="utf-8")) == {"run_id": "run-1", "failed_inputs": 1}

    record = _gate_run(tmp_path)
    assert record["failed_inputs"] == 1
    measures = {"failed_inputs": record["failed_inputs"]}
    fails = gate.evaluate(measures, {"max_failed_inputs": 0})
    assert any("max_failed_inputs" in x for x in fails)
    assert gate.is_gated("pmc", "extract")
    assert gate.load_rules("pmc")["extract"]["max_failed_inputs"] == 0


def test_a_clean_run_records_zero(tmp_path, env):
    raw, out = env
    _article(raw, 1)
    assert _extract(raw, out) == 0
    assert _gate_run(tmp_path)["failed_inputs"] == 0


def test_a_file_of_another_run_is_ignored(tmp_path, env):
    raw, out = env
    f = ops_root(out, "pmc") / FAILED_INPUTS_FILE
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"run_id": "other", "failed_inputs": 5}), encoding="utf-8")
    assert "failed_inputs" not in _gate_run(tmp_path)


def test_a_source_without_the_file_is_unaffected(tmp_path, env):
    assert "failed_inputs" not in _gate_run(tmp_path, source="apollo")
    assert "failed_inputs" not in _gate_run(tmp_path, source=None)
