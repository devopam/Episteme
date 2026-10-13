"""workers=1 (in-process) and workers=3 (spawned processes) write the same rows;
the production `python -m` entry point works with worker processes."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SP2 = REPO / "tests" / "fixtures" / "sp2"
CASES = [
    ("episteme.data.pubmed.extract_pubmed", "extract_pubmed", SP2 / "pubmed", "pubmed"),
    (
        "episteme.data.pmc.extract_pmc",
        "extract_pmc",
        REPO / "tests" / "data" / "fixtures" / "pmc",
        "pmc",
    ),
    (
        "episteme.data.bookshelf.extract_bookshelf",
        "extract_bookshelf",
        SP2 / "bookshelf",
        "bookshelf",
    ),
    (
        "episteme.data.guidelines.extract_guidelines",
        "extract_guidelines",
        SP2 / "guidelines",
        "guidelines",
    ),
    ("episteme.data.apollo.extract_apollo", "extract_apollo", SP2 / "apollo", "apollo"),
    (
        "episteme.data.europepmc.preprints.extract_europepmc_preprints",
        "extract_europepmc_preprints",
        SP2 / "europepmc_preprint",
        "europepmc_preprint",
    ),
    (
        "episteme.data.europepmc.manuscripts.extract_europepmc_manuscripts",
        "extract_europepmc_manuscripts",
        SP2 / "europepmc_manuscript",
        "europepmc_manuscript",
    ),
    (
        "episteme.data.openalex.serialize_openalex",
        "serialize_openalex",
        REPO / "tests" / "fixtures" / "sp4" / "openalex",
        "openalex",
    ),
]
# Wall-clock stamp written into every row; differs between any two runs.
_VOLATILE = {"retrieved_at"}


def _rows(processed: Path, source: str) -> list[dict]:
    import pyarrow.parquet as pq

    staging = processed / "staging" / source
    rows: list[dict] = []
    for p in sorted(staging.glob("*.parquet")):
        rows += pq.read_table(p).to_pylist()
    for p in sorted(staging.glob("*.jsonl")):
        rows += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    kept = [{k: v for k, v in r.items() if k not in _VOLATILE} for r in rows]
    return sorted(kept, key=lambda r: (r["id"], json.dumps(r, sort_keys=True, default=str)))


@pytest.mark.parametrize("module,func,raw_dir,source", CASES, ids=[c[3] for c in CASES])
def test_workers_3_writes_the_same_rows_as_workers_1(
    tmp_path, spawn_child_env, module, func, raw_dir, source
):
    fn = getattr(importlib.import_module(module), func)
    one = fn(raw_dir, tmp_path / "one", workers=1)
    three = fn(raw_dir, tmp_path / "three", workers=3)
    assert three == one  # {"inputs", "ok", "failed", "rows"}
    rows_one = _rows(tmp_path / "one", source)
    assert rows_one, "fixture produced no rows"
    assert _rows(tmp_path / "three", source) == rows_one


def _cli(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess:
    # cwd=tmp_path: no pyproject.toml above it, so the child loads no .env.
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "episteme.data.pubmed.extract_pubmed",
            "--raw-dir",
            str(SP2 / "pubmed"),
            "--processed-dir",
            str(tmp_path / "cli"),
            *extra,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_cli_module_entry_point_with_worker_processes(tmp_path, spawn_child_env):
    """Production runs `python -m ...`: process_one then lives in __main__ and
    spawn must re-import it in each worker."""
    proc = _cli(tmp_path, "--workers", "2")
    assert proc.returncode == 0, proc.stderr
    assert "workers=2" in proc.stdout
    assert _rows(tmp_path / "cli", "pubmed")


def test_cli_workers_default_comes_from_the_setting(tmp_path, spawn_child_env, monkeypatch):
    monkeypatch.setenv("EPISTEME_EXTRACT_WORKERS", "2")
    proc = _cli(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "workers=2" in proc.stdout
