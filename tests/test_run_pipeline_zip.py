"""run_pipeline.sh --zip: forwarded to ``aact load`` only, so the gate pass check applies.

No DB, no network. Fake stage wrappers, as in test_run_pipeline_skip_unit.py."""

from __future__ import annotations

import pytest

from test_run_pipeline_skip_unit import FAKE_STAGE, _run, _write
from test_run_pipeline_skip_unit import tree as _tree  # noqa: F401 - fixture


@pytest.fixture
def tree(_tree):  # noqa: F811
    script = _tree[0]
    _write(script.parent / "aact" / "load_aact.sh", FAKE_STAGE)
    _write(script.parent / "pubmed" / "load_pubmed.sh", FAKE_STAGE)
    return _tree


def test_zip_reaches_the_aact_load(tree):
    proc, calls = _run(
        tree,
        "aact",
        "load",
        "--zip",
        "/raw/aact/20261003_export_ctgov.zip",
        "--skip-gate",
        "--reason",
        "t",
    )
    assert proc.returncode == 0, proc.stderr
    assert calls == ["load_aact.sh --zip /raw/aact/20261003_export_ctgov.zip"]


def test_zip_is_refused_for_another_source_and_stage(tree):
    proc, calls = _run(tree, "pubmed", "load", "--zip", "x.zip")
    assert (
        proc.returncode == 2 and "--zip is valid with aact load only" in proc.stderr + proc.stdout
    )
    assert calls == []
    proc, calls = _run(tree, "aact", "download", "--zip", "x.zip")
    assert proc.returncode == 2 and calls == []


def test_zip_is_refused_with_sample_and_without_a_path(tree):
    proc, calls = _run(tree, "aact", "load", "--zip", "x.zip", "--sample")
    assert proc.returncode == 2 and "--sample and --zip" in proc.stderr + proc.stdout
    assert calls == []
    proc, calls = _run(tree, "aact", "load", "--zip")
    assert proc.returncode == 2 and calls == []
