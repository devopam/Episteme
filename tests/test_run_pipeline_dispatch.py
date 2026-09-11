"""Contract smoke test for scripts/data/run_pipeline.sh dispatch.

Shell-only (no DB): most assertions run with --dry-run, which run_pipeline.sh
handles *before* the audit bracket, so Postgres is never touched. The one
exception, `test_bookshelf_extract_dispatches`, drives a real (non-dry-run)
`bookshelf extract` — its `audit_trail record` targets `PGDATABASE=episteme_test`
and degrades to a `log WARN` (no exception) when no DB is up, so the test still
runs DB-free. Not marked `pg` — the whole module runs under `pytest -m "not pg"`.

Guards the dispatch table that 21 sources + the SP3 exit criteria rest on, plus
regression guards for the fix wave:
  * FIX 2 (C2) — `pmc extract --dry-run` must die 3.
  * FIX 3 (I1) — a bad MODE positional now reaches the wrapper's own `die`.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "data" / "run_pipeline.sh"


def _find_bash() -> str:
    """A real POSIX bash — never Windows' WSL shim in System32/WindowsApps."""
    for cand in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        "/usr/bin/bash",
        "/bin/bash",
    ):
        if Path(cand).exists():
            return cand
    found = shutil.which("bash")
    if found and "System32" not in found and "WindowsApps" not in found:
        return found
    return ""


BASH = _find_bash()
if not BASH:  # pragma: no cover - environment dependent
    pytest.skip("no usable POSIX bash for the dispatch smoke test", allow_module_level=True)

# Fast tokens: their download-dry-run resolves a small listing quickly.
FAST_TOKENS = [
    "pmc",
    "apollo",
    "chembl",
    "uniprot",
    "pubchem",
    "clinvar",
    "reactome",
    "mesh",
    "ontologies",
    "openalex",
    "hf_corpus",
    "dailymed",
    "aact",
    "europepmc_preprint",
    "europepmc_id_mappings",
    "europepmc_lite",
    "europepmc_abstracts",
]
# Heavy tokens: large upstream manifests / slow directory listings.
HEAVY_TOKENS = ["bookshelf", "openfda", "pubmed", "europepmc_manuscript"]

FAST_TIMEOUT = 240
HEAVY_TIMEOUT = 600


def _online() -> bool:
    for host in ("ftp.ebi.ac.uk", "ftp.ncbi.nlm.nih.gov"):
        try:
            socket.create_connection((host, 443), timeout=5).close()
            return True
        except OSError:
            continue
    return False


if not _online():  # pragma: no cover - environment dependent
    pytest.skip(
        "no network — run_pipeline.sh dispatch smoke test needs upstream listings",
        allow_module_level=True,
    )


def _run(args: list[str], tmp_path: Path, timeout: int) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "EPISTEME_ACTOR": "episteme_sys_admin",
        "PGDATABASE": "episteme_test",
        "EPISTEME_DATA_ROOT": str(tmp_path),
    }
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _assert_download_dry_run_dispatches(token: str, tmp_path: Path, timeout: int) -> None:
    """rc 0 is the contract. rc 1 == the wrapper's own `die` (an upstream listing
    timed out / changed) — retry once, then skip rather than fail: this is a
    dispatch smoke test, not an upstream-availability test. rc 2/3 (bad
    source/stage, wrapper missing) is a real dispatch regression -> fail."""
    for attempt in (1, 2):
        try:
            proc = _run([token, "download", "--dry-run"], tmp_path, timeout)
        except subprocess.TimeoutExpired:
            pytest.skip(f"{token}: dry-run exceeded {timeout}s (slow network)")
        if proc.returncode == 0:
            return
        if proc.returncode != 1:
            raise AssertionError(
                f"{token} download --dry-run rc={proc.returncode} "
                f"(dispatch bug)\n{proc.stderr[-2000:]}"
            )
        if attempt == 2:
            pytest.skip(
                f"{token}: wrapper failed transiently (rc=1, likely upstream)\n{proc.stderr[-800:]}"
            )


@pytest.mark.parametrize("token", FAST_TOKENS)
def test_download_dry_run_dispatches(token: str, tmp_path: Path) -> None:
    _assert_download_dry_run_dispatches(token, tmp_path, FAST_TIMEOUT)


@pytest.mark.slow
@pytest.mark.parametrize("token", HEAVY_TOKENS)
def test_download_dry_run_dispatches_heavy(token: str, tmp_path: Path) -> None:
    _assert_download_dry_run_dispatches(token, tmp_path, HEAVY_TIMEOUT)


def test_non_download_stage_for_table_source_dies_3(tmp_path: Path) -> None:
    proc = _run(["chembl", "extract"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3, proc.stderr[-2000:]


def test_unknown_source_dies_3(tmp_path: Path) -> None:
    proc = _run(["bogus", "download"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3, proc.stderr[-2000:]


def test_pmc_non_download_dry_run_dies_3(tmp_path: Path) -> None:
    # FIX 2 (C2) regression guard — SP2 generalised the message (PF-8.1).
    proc = _run(["pmc", "extract", "--dry-run"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3, proc.stderr[-2000:]
    assert "--dry-run is supported on 'download' only" in proc.stderr


def test_bookshelf_extract_dry_run_dies_3(tmp_path: Path) -> None:
    # SP2 PF-8.1: --dry-run rejected on every write stage, every source.
    proc = _run(["bookshelf", "extract", "--dry-run"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3, proc.stderr[-2000:]
    assert "--dry-run is supported on 'download' only" in proc.stderr


def test_bookshelf_extract_dispatches(tmp_path: Path) -> None:
    # no data -> the (scaffold) extractor exits 0 (nothing to do) or 1 (no raw
    # dir); NOT 3 (a dispatch bug: wrapper missing / stage not wired).
    proc = _run(["bookshelf", "extract", "--max-files", "1"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode in (0, 1), proc.stderr[-2000:]


def test_corpus_materialize_dry_run_dies_3(tmp_path: Path) -> None:
    # `corpus` is the materialize-only alias; --dry-run on it hits PF-8.1.
    proc = _run(["corpus", "materialize", "--dry-run"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3, proc.stderr[-2000:]
    assert "--dry-run is supported on 'download' only" in proc.stderr


def test_europepmc_id_mappings_extract_dies_3(tmp_path: Path) -> None:
    # RULING (Task 11): europepmc_id_mappings is not in LIT_SOURCES -- it gets
    # a `load`-only exception, not the full extract/load/graph chain.
    proc = _run(["europepmc_id_mappings", "extract"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3, proc.stderr[-2000:]


def test_europepmc_id_mappings_load_dry_run_dies_3(tmp_path: Path) -> None:
    # PF-8.1 (generalised): --dry-run rejected on every write stage, every
    # source -- including the europepmc_id_mappings `load` exception arm.
    proc = _run(["europepmc_id_mappings", "load", "--dry-run"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3, proc.stderr[-2000:]
    assert "--dry-run is supported on 'download' only" in proc.stderr


def test_europepmc_lite_extract_dies_3(tmp_path: Path) -> None:
    # Task 12: europepmc_lite is not in LIT_SOURCES -- it gets an
    # `enrich`-only exception (like Task 11's `load`-only one), not the full
    # extract/load/graph chain. Assert the exact stderr substring, not just
    # rc==3 -- a bare rc check can't tell "gate correctly rejected this" from
    # "gate removed, something else died 3 instead" (Task 11 review lesson).
    proc = _run(["europepmc_lite", "extract"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3, proc.stderr[-2000:]
    assert "europepmc_lite extract is not in SP2" in proc.stderr


def test_europepmc_lite_enrich_dry_run_dies_3(tmp_path: Path) -> None:
    # PF-8.1 (generalised): --dry-run rejected on every write stage, every
    # source -- including the new europepmc_lite `enrich` exception arm.
    proc = _run(["europepmc_lite", "enrich", "--dry-run"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3, proc.stderr[-2000:]
    assert "--dry-run is supported on 'download' only" in proc.stderr


def test_openalex_bad_mode_reaches_wrapper_die(tmp_path: Path) -> None:
    # FIX 3 (I1) regression guard: the positional now reaches openalex's F-2 die.
    try:
        proc = _run(["openalex", "download", "--dry-run", "bogusmode"], tmp_path, FAST_TIMEOUT)
    except subprocess.TimeoutExpired:
        pytest.skip("openalex bogusmode: timed out")
    assert proc.returncode == 1, proc.stderr[-2000:]
    assert "unknown mode 'bogusmode'" in proc.stderr
