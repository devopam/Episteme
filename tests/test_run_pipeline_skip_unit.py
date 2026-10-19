"""run_pipeline.sh --skip-unit: validation and forwarding, against fake scripts.

No DB, no network. Same pattern as test_run_pipeline_bulk.py: a copy of
scripts/data with fake stage wrappers that log their arguments, and a fake
PYTHON for the audit calls."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from tests.test_run_pipeline_bulk import BASH, FAKE_PY, REPO_ROOT, _write

FAKE_STAGE = """#!/usr/bin/env bash
echo "$(basename "$0") $*" >> "$CALLS"
exit 0
"""


@pytest.fixture
def tree(tmp_path):
    data = tmp_path / "tree" / "scripts" / "data"
    shutil.copytree(REPO_ROOT / "scripts" / "data", data)
    for rel in (
        "pubmed/extract_pubmed.sh",
        "pubmed/load_pubmed.sh",
        "pubmed/graph_pubmed.sh",
        "chembl/serialize_chembl.sh",
    ):
        _write(data / rel, FAKE_STAGE)
    fake_py = tmp_path / "fakepy"
    _write(fake_py, FAKE_PY)
    return data / "run_pipeline.sh", tmp_path / "calls.log", fake_py


def _run(tree, source: str, stage: str, *args: str):
    script, calls, fake_py = tree
    env = {k: v for k, v in os.environ.items() if not k.startswith(("EPISTEME_", "PG"))}
    env.update(
        EPISTEME_ACTOR="pytest",
        PYTHON=fake_py.as_posix(),
        CALLS=calls.as_posix(),
        EPISTEME_DB_MODE="restricted",
        PGDATABASE="episteme_spawn_blocked",
        EPISTEME_PRODUCTION_DATABASE="episteme_spawn_blocked",
    )
    proc = subprocess.run(
        [BASH, str(script), source, stage, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return proc, [ln.rstrip() for ln in lines if ln not in ("run_start", "run_end")]


def test_extract_forwards_every_skip_unit_and_the_reason(tree):
    proc, calls = _run(
        tree, "pubmed", "extract", "--skip-unit", "a.xml", "--skip-unit", "b.xml", "--reason", "bad"
    )
    assert proc.returncode == 0, proc.stderr
    assert calls == ["extract_pubmed.sh --skip-unit a.xml --skip-unit b.xml --reason bad"]


def test_serialize_forwards_the_skip_unit(tree):
    proc, calls = _run(tree, "chembl", "serialize", "--skip-unit", "x", "--reason", "r")
    assert proc.returncode == 0, proc.stderr
    assert calls == ["serialize_chembl.sh --skip-unit x --reason r"]


def test_load_forwards_the_skip_unit_to_load_only(tree):
    proc, calls = _run(tree, "pubmed", "load", "--skip-unit", "s.jsonl", "--reason", "r")
    assert proc.returncode == 0, proc.stderr
    assert calls == ["load_pubmed.sh --skip-unit s.jsonl --reason r"]


def test_no_skip_unit_forwards_nothing(tree):
    proc, calls = _run(tree, "pubmed", "extract")
    assert proc.returncode == 0, proc.stderr
    assert calls == ["extract_pubmed.sh"]


@pytest.mark.parametrize(
    ("stage", "args"),
    [
        ("extract", ["--skip-unit", "a"]),  # no --reason
        ("all", ["--skip-unit", "a", "--reason", "r"]),  # not one named stage
        ("graph", ["--skip-unit", "a", "--reason", "r"]),  # graph has no skip
    ],
)
def test_skip_unit_misuse_exits_2_and_runs_no_stage(tree, stage, args):
    proc, calls = _run(tree, "pubmed", stage, *args)
    assert proc.returncode == 2, proc.stderr
    assert calls == []


@pytest.mark.parametrize("source", ["europepmc_id_mappings", "cdisc_ct"])
def test_skip_unit_is_rejected_for_loaders_that_do_not_take_it(tree, source):
    proc, calls = _run(tree, source, "load", "--skip-unit", "a", "--reason", "r")
    assert proc.returncode == 2, proc.stderr
    assert "does not support --skip-unit" in proc.stderr
    assert calls == []


def test_force_and_skip_unit_pass_reason_once(tree):
    proc, calls = _run(
        tree, "pubmed", "load", "--force", "--skip-unit", "s.jsonl", "--reason", "why"
    )
    assert proc.returncode == 0, proc.stderr
    assert calls == ["load_pubmed.sh --force --reason why --skip-unit s.jsonl"]
