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
import re
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


def test_mesh_download_dry_run_resolves_a_descriptor_release(tmp_path: Path) -> None:
    # SP4.1 Task 7: the wrapper resolved 0 files against NLM's real layout.
    try:
        proc = _run(["mesh", "download", "--dry-run"], tmp_path, FAST_TIMEOUT)
    except subprocess.TimeoutExpired:
        pytest.skip("mesh: dry-run exceeded timeout (slow network)")
    if proc.returncode == 1:
        pytest.skip(f"mesh: wrapper failed transiently (rc=1): {proc.stderr[-800:]}")
    assert proc.returncode == 0, proc.stderr[-2000:]
    if "resolved 0 files" in proc.stderr:
        codes = re.findall(r"desc\d{4}\.(?:gz|xml)=(\d{3})", proc.stderr)
        if codes and all(c == "000" for c in codes):
            pytest.skip("mesh: every probe failed at the network level (NLM unreachable)")
        pytest.fail(f"NLM reachable but no descriptor release resolved: {proc.stderr[-2000:]}")
    assert re.search(
        r"would fetch \S+/desc\d{4}\.gz -> \S+/desc\d{4}\.gz", proc.stderr
    ), proc.stderr[-2000:]


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


def test_chembl_serialize_dry_run_dies_3(tmp_path):
    proc = _run(["chembl", "serialize", "--dry-run"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3, proc.stderr[-2000:]
    assert "writes the DB" in proc.stderr


def test_chembl_serialize_dispatches(tmp_path):
    # no data -> the scaffold exits 0 (nothing to do); NOT 3 (dispatch bug)
    proc = _run(["chembl", "serialize", "--max-files", "1"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode in (0, 1), proc.stderr[-2000:]


def test_uniprot_serialize_dispatches(tmp_path):
    # SP4 Task 6 added the real scripts/data/uniprot/serialize_uniprot.sh,
    # closing the [ -f ] guard gap this test used to pin (it used to assert
    # rc==3 / "wrapper not found" -- see git history, and Task 10's
    # test_mesh_graph_dispatches for the identical precedent). Now uniprot
    # serialize actually dispatches: against an empty tmp_path data root,
    # the wrapper's own raw-dir existence check fails with rc 1 ("raw dir
    # not found"), not rc 3 -- proves _is_structured("uniprot") is true and
    # dispatch reaches inside the real wrapper.
    proc = _run(["uniprot", "serialize"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 1, proc.stderr[-2000:]
    assert "raw dir not found" in proc.stderr


def test_mesh_graph_dispatches(tmp_path):
    # SP4 Task 10 added the real scripts/data/mesh/graph_mesh.sh, closing the
    # [ -f ] guard gap this test used to pin (it used to assert rc==3 /
    # "wrapper not found" -- see git history). Now the mesh-specific graph
    # carve-out actually dispatches: against an empty tmp_path data root,
    # graph_builder.build() finds zero matching source_files and exits 0
    # cleanly (same "no data -> 0/1, never 3" idiom as
    # test_bookshelf_extract_dispatches above).
    proc = _run(["mesh", "graph"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode in (0, 1), proc.stderr[-2000:]
    assert "stage: graph" in proc.stderr


def test_pmc_serialize_dies_3(tmp_path: Path) -> None:
    # FIX 2 (whole-branch review): `serialize` is a valid top-level STAGE (SP4
    # added it to the general allow-list for the 8 structured sources), but
    # pmc's own dispatch case-block has no arm for it -- pre-fix this silently
    # no-ops with rc 0 after writing a run_start/run_end audit-row pair for a
    # command that did zero work. Must die 3, mirroring `corpus`'s existing
    # catch-all precedent (both its early-validation guard and its
    # belt-and-braces case arm).
    #
    # Asserts "(early validation)" specifically, not just "not wired": the
    # early guard and the belt-and-braces case-arm die with distinguishable
    # messages on purpose (see run_pipeline.sh's comment on the early guard)
    # so this test pins which one actually fired. A generic "not wired"
    # check would still pass if the early guard were ever deleted -- the
    # case-arm alone would still reject with rc 3 -- silently losing the
    # property this guard exists for: that pmc's run_start audit call never
    # fires for an unwired stage. A re-review of the original fix flagged
    # this exact gap (this branch was already bitten once by an
    # under-discriminating dispatch test, see test_uniprot_serialize_dispatches).
    proc = _run(["pmc", "serialize"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3, proc.stderr[-2000:]
    assert "not wired (early validation)" in proc.stderr


def test_pubchem_serialize_is_not_a_literature_source(tmp_path):
    # a structured source must NOT be reachable via the SP2 _is_lit gate
    proc = _run(["pubchem", "extract"], tmp_path, FAST_TIMEOUT)
    assert proc.returncode == 3, proc.stderr[-2000:]
    assert "not in SP2" in proc.stderr or "not wired" in proc.stderr


def _run_env(args: list[str], tmp_path: Path, timeout: int, **env_over: str):
    env = {
        **os.environ,
        "EPISTEME_ACTOR": "episteme_sys_admin",
        "EPISTEME_DATA_ROOT": str(tmp_path),
        **env_over,
    }
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_restricted_mode_refuses_production_database(tmp_path: Path) -> None:
    # PGPORT=1: even if the guard were ever broken, no real connection to any
    # database can be opened by this test.
    proc = _run_env(
        ["chembl", "serialize"],
        tmp_path,
        FAST_TIMEOUT,
        PGDATABASE="episteme",
        EPISTEME_DB_MODE="restricted",
        PGPORT="1",
    )
    assert "Refusing to open a connection to production database" in proc.stderr
    assert "connection refused" not in proc.stderr.lower()


def test_restricted_mode_allows_secondary_target(tmp_path: Path) -> None:
    proc = _run_env(
        ["chembl", "serialize"],
        tmp_path,
        FAST_TIMEOUT,
        PGDATABASE="episteme",
        PGDATABASE_SECONDARY="episteme_test",
        EPISTEME_DB_TARGET="secondary",
        EPISTEME_DB_MODE="restricted",
        PGPORT="1",
    )
    assert "Refusing to open a connection to production database" not in proc.stderr
