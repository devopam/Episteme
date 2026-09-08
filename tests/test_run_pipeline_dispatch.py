"""Contract smoke test for scripts/data/run_pipeline.sh dispatch.

Shell-only (no DB): every assertion runs with --dry-run, which run_pipeline.sh
handles *before* the audit bracket, so Postgres is never touched. Not marked
`pg` — it runs under `pytest -m "not pg"`.

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
    # FIX 2 (C2) regression guard.
    proc = _run(["pmc", "extract", "--dry-run"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3, proc.stderr[-2000:]
    assert "no --dry-run" in proc.stderr


def test_openalex_bad_mode_reaches_wrapper_die(tmp_path: Path) -> None:
    # FIX 3 (I1) regression guard: the positional now reaches openalex's F-2 die.
    try:
        proc = _run(["openalex", "download", "--dry-run", "bogusmode"], tmp_path, FAST_TIMEOUT)
    except subprocess.TimeoutExpired:
        pytest.skip("openalex bogusmode: timed out")
    assert proc.returncode == 1, proc.stderr[-2000:]
    assert "unknown mode 'bogusmode'" in proc.stderr
