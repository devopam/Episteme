"""run_pipeline.sh: --sample, the gate check on full runs and --skip-gate -- against fakes.

No DB, no network. A copy of scripts/data with fake stage wrappers that log
their environment. The fake PYTHON logs the audit calls (with the database
target they would go to), answers the gate ``check``, ``prepare``, ``report``
and ``migrations-check`` commands from the environment, and hands every other
``episteme.ops.*`` call (``gate paths``, ``gate run``, the lock, the status) to
the real interpreter. Every root (data, raw, processed, corpus, lock folder) is
a temp path (controller ruling R2)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from test_run_pipeline_bulk import BASH, REPO_ROOT, _write
from test_run_pipeline_lock_status import REAL_PY, _foreign_lock

# CHECK_<stage>: the answer of `gate check` ("ok" exits 0, any other answer 1);
# unset -> the real `gate check`. RC_REPORT: the exit code of `gate report`.
# MISSING_MIG: a migration that `gate migrations-check` reports missing.
FAKE_PY = """#!/usr/bin/env bash
db="${EPISTEME_DB_TARGET:-primary}"
case "$*" in
    *audit_trail*run_start*) echo "run_start $db" >> "$AUDIT"; exit 0 ;;
    *audit_trail*run_end*) echo "run_end $db $*" >> "$AUDIT"; exit 0 ;;
    *audit_trail*gate_override*)
        echo "gate_override $db $*" >> "$AUDIT"; exit "${RC_OVERRIDE:-0}" ;;
    *audit_trail*) exit 0 ;;
    *"episteme.ops.gate check"*)
        args=("$@"); st=""
        while [ $# -gt 0 ]; do [ "$1" = --stage ] && st="$2"; shift; done
        echo "check $st" >> "$CALLS"
        var="CHECK_$st"
        [ -n "${!var:-}" ] || exec "$REAL_PY" "${args[@]}"
        echo "${!var}"
        [ "${!var}" = ok ] && exit 0
        exit 1 ;;
    *"episteme.ops.gate prepare"*)
        echo "prepare $db $*" >> "$CALLS"
        sr=""
        while [ $# -gt 0 ]; do [ "$1" = --sample-root ] && sr="$2"; shift; done
        [ -z "${RC_PREPARE:-}" ] || exit "$RC_PREPARE"
        mkdir -p "$sr/01_raw/bookshelf"
        echo '{"files": []}' > "$sr/slice.json"
        echo "$sr"
        exit 0 ;;
    *"episteme.ops.gate report"*)
        echo "report $db $*" >> "$CALLS"
        pr=""; src=""; st=""
        while [ $# -gt 0 ]; do
            case "$1" in --pass-root) pr="$2" ;; --source) src="$2" ;; --stage) st="$2" ;; esac
            shift
        done
        if [ -n "${RC_REPORT:-}" ]; then
            echo "gate FAIL for $src $st:"
            echo "  min_rows: rows is 0, the minimum is 1"
            exit "$RC_REPORT"
        fi
        mkdir -p "$pr/_ops/$src/gate"
        echo pass > "$pr/_ops/$src/gate/$st.pass"
        echo "gate PASS for $src $st"
        exit 0 ;;
    *"episteme.ops.gate migrations-check"*)
        echo "migcheck $db" >> "$CALLS"
        if [ -n "${MISSING_MIG:-}" ]; then echo "$MISSING_MIG: missing episteme.x"; exit 1; fi
        exit 0 ;;
    *"-m episteme.ops."*) exec "$REAL_PY" "$@" ;;
esac
exit 0
"""

# Logs its name, the roots and database target it sees, the lock files and
# whether it was started by `gate run` (a Python parent); exits with $RC_<name>.
FAKE_STAGE = """#!/usr/bin/env bash
name="$(basename "$0" .sh)"
echo "stage $name" >> "$CALLS"
for k in EPISTEME_DATA_ROOT EPISTEME_RAW_ROOT EPISTEME_PROCESSED_ROOT EPISTEME_CORPUS_ROOT \\
         EPISTEME_LOCK_DIR EPISTEME_DB_TARGET; do
    echo "env $k ${!k:-}" >> "$CALLS"
done
f="$EPISTEME_LOCK_DIR/heavy.lock"
[ -f "$f" ] && echo "lock heavy $(tr -d '\\r\\n' < "$f")" >> "$CALLS"
rc_var="RC_${name}"
exit "${!rc_var:-0}"
"""

STAGES = (
    "bookshelf/download_bookshelf.sh",
    "bookshelf/extract_bookshelf.sh",
    "bookshelf/load_bookshelf.sh",
    "bookshelf/graph_bookshelf.sh",
    "pubmed/extract_pubmed.sh",
)


@pytest.fixture
def tree(tmp_path):
    data = tmp_path / "tree" / "scripts" / "data"
    shutil.copytree(REPO_ROOT / "scripts" / "data", data)
    for rel in STAGES:
        _write(data / rel, FAKE_STAGE)
    _write(tmp_path / "fakepy", FAKE_PY)
    return data, tmp_path


def _roots(tmp: Path) -> dict[str, Path]:
    d = tmp / "data"
    return {
        "EPISTEME_DATA_ROOT": d,
        "EPISTEME_RAW_ROOT": d / "01_raw",
        "EPISTEME_PROCESSED_ROOT": d / "02_processed",
        "EPISTEME_CORPUS_ROOT": d / "03_corpus",
        "EPISTEME_LOCK_DIR": d / "_ops" / "locks",
    }


def _run(tree, source: str, stage: str, *args: str, **env_over: str):
    data, tmp = tree
    env = {k: v for k, v in os.environ.items() if not k.startswith(("EPISTEME_", "PG"))}
    env.update({k: v.as_posix() for k, v in _roots(tmp).items()})
    env.update(
        EPISTEME_ACTOR="pytest",
        PYTHON=(tmp / "fakepy").as_posix(),
        REAL_PY=REAL_PY,
        CALLS=(tmp / "calls.log").as_posix(),
        AUDIT=(tmp / "audit.log").as_posix(),
        EPISTEME_DB_MODE="restricted",
        PGDATABASE="episteme_spawn_blocked",
        PGDATABASE_SECONDARY="episteme_spawn_blocked",
        EPISTEME_PRODUCTION_DATABASE="episteme_spawn_blocked",
    )
    env.update(env_over)
    proc = subprocess.run(
        [BASH, str(data / "run_pipeline.sh"), source, stage, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return proc, _lines(tmp / "calls.log"), _lines(tmp / "audit.log")


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _env(calls: list[str]) -> dict[str, str]:
    out = {}
    for c in calls:
        if c.startswith("env "):
            _, key, *val = c.split(" ", 2)
            out[key] = val[0] if val else ""
    return out


def _same(a: str | Path, b: str | Path) -> bool:
    return Path(a).resolve() == Path(b).resolve()


def _arg(line: str, flag: str) -> str:
    """The value after ``flag`` in a logged command line (no spaces in temp paths)."""
    parts = line.split()
    return parts[parts.index(flag) + 1]


def _sample_root(tmp: Path) -> Path:
    return _roots(tmp)["EPISTEME_PROCESSED_ROOT"] / "_sample" / "bookshelf"


def _stages(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("stage ")]


def _no_runs(tmp: Path) -> bool:
    return not (_roots(tmp)["EPISTEME_PROCESSED_ROOT"] / "_ops" / "runs").exists()


# --- the gate check on full runs ---------------------------------------------


def test_a_full_run_without_a_pass_refuses_with_2_and_no_audit_row(tree):
    proc, calls, audit = _run(tree, "bookshelf", "extract", CHECK_extract="no pass")
    assert proc.returncode == 2, proc.stderr
    assert "no pass" in proc.stderr and "--sample" in proc.stderr
    assert audit == []
    assert _stages(calls) == []
    assert _no_runs(tree[1])
    assert not (_roots(tree[1])["EPISTEME_LOCK_DIR"] / "heavy.lock").exists()


@pytest.mark.parametrize("reason", ["code changed", "rules changed"])
def test_a_stale_pass_refuses_with_its_reason(tree, reason):
    proc, calls, audit = _run(tree, "bookshelf", "extract", CHECK_extract=reason)
    assert proc.returncode == 2, proc.stderr
    assert reason in proc.stderr
    assert audit == [] and _stages(calls) == []


def test_a_valid_pass_lets_the_run_go_on(tree):
    proc, calls, audit = _run(tree, "bookshelf", "extract", CHECK_extract="ok")
    assert proc.returncode == 0, proc.stderr
    assert _stages(calls) == ["stage extract_bookshelf"]
    assert audit[0] == "run_start primary"
    assert not any(a.startswith("gate_override") for a in audit)
    env = _env(calls)
    assert _same(env["EPISTEME_PROCESSED_ROOT"], _roots(tree[1])["EPISTEME_PROCESSED_ROOT"])


def test_skip_gate_writes_one_gate_override_row_to_production_then_runs(tree):
    proc, calls, audit = _run(
        tree,
        "bookshelf",
        "extract",
        "--skip-gate",
        "--reason",
        "x",
        CHECK_extract="code changed",
    )
    assert proc.returncode == 0, proc.stderr
    overrides = [a for a in audit if a.startswith("gate_override")]
    assert len(overrides) == 1
    assert overrides[0].startswith("gate_override primary ")
    assert "--object bookshelf extract" in overrides[0]
    assert overrides[0].endswith("--reason x")
    assert audit.index(overrides[0]) < audit.index("run_start primary")
    assert _stages(calls) == ["stage extract_bookshelf"]


def test_skip_gate_with_a_valid_pass_writes_no_row(tree):
    proc, _, audit = _run(
        tree, "bookshelf", "extract", "--skip-gate", "--reason", "x", CHECK_extract="ok"
    )
    assert proc.returncode == 0, proc.stderr
    assert not any(a.startswith("gate_override") for a in audit)


def test_a_failed_gate_override_row_stops_the_run(tree):
    proc, calls, audit = _run(
        tree,
        "bookshelf",
        "extract",
        "--skip-gate",
        "--reason",
        "x",
        CHECK_extract="no pass",
        RC_OVERRIDE="1",
    )
    assert proc.returncode == 1, proc.stderr
    assert "gate_override" in proc.stderr
    assert "run_start primary" not in audit
    assert _stages(calls) == []
    assert not (_roots(tree[1])["EPISTEME_LOCK_DIR"] / "heavy.lock").exists()


def test_skip_gate_without_a_reason_is_a_usage_error(tree):
    proc, calls, audit = _run(tree, "bookshelf", "extract", "--skip-gate")
    assert proc.returncode == 2, proc.stderr
    assert "--skip-gate requires --reason" in proc.stderr
    assert calls == [] and audit == []


def test_all_checks_every_stage_up_front_and_refuses_before_run_start(tree):
    proc, calls, audit = _run(
        tree, "bookshelf", "all", CHECK_extract="ok", CHECK_load="no pass", CHECK_graph="ok"
    )
    assert proc.returncode == 2, proc.stderr
    assert "load (no pass)" in proc.stderr
    assert [c for c in calls if c.startswith("check ")] == [
        "check download",
        "check extract",
        "check load",
        "check graph",
    ]
    assert _stages(calls) == []
    assert audit == []


def test_all_with_skip_gate_writes_one_row_per_stage_without_a_pass(tree):
    proc, calls, audit = _run(
        tree,
        "bookshelf",
        "all",
        "--skip-gate",
        "--reason",
        "r",
        CHECK_extract="no pass",
        CHECK_load="code changed",
        CHECK_graph="ok",
    )
    assert proc.returncode == 0, proc.stderr
    objects = [
        re.search(r"--object (.+?) --run-id", a).group(1)
        for a in audit
        if a.startswith("gate_override")
    ]
    assert objects == ["bookshelf extract", "bookshelf load"]
    assert len(_stages(calls)) == 4


def test_an_ungated_source_is_unaffected(tree):
    """The real `gate check`: pubmed has no rules file, so it is not gated."""
    proc, calls, audit = _run(tree, "pubmed", "extract")
    assert proc.returncode == 0, proc.stderr
    assert calls[0] == "check extract"
    assert _stages(calls) == ["stage extract_pubmed"]
    assert audit[0] == "run_start primary"


def test_a_dry_run_is_never_gated(tree):
    proc, calls, _ = _run(tree, "bookshelf", "download", "--dry-run", CHECK_download="no pass")
    assert proc.returncode == 0, proc.stderr
    assert not any(c.startswith("check ") for c in calls)


# --- --sample ----------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ("all", "--sample"),
        ("download", "--sample"),
        ("graph", "--sample"),
        ("load", "--sample", "--bulk"),
        ("extract", "--sample", "--skip-gate", "--reason", "r"),
        ("extract", "--sample", "--force", "--reason", "r"),
        ("extract", "--sample", "--dry-run"),
    ],
)
def test_sample_misuse_is_a_usage_error(tree, args):
    proc, calls, audit = _run(tree, "bookshelf", *args, CHECK_extract="no pass")
    assert proc.returncode == 2, proc.stderr
    assert "--sample" in proc.stderr
    assert _stages(calls) == [] and audit == []


def test_sample_of_an_ungated_stage_is_refused(tree):
    proc, calls, audit = _run(tree, "pubmed", "extract", "--sample")
    assert proc.returncode == 2, proc.stderr
    assert "no gate rules" in proc.stderr
    assert _stages(calls) == [] and audit == []


def test_sample_extract_redirects_the_roots_but_not_the_data_root_or_lock(tree):
    tmp = tree[1]
    roots = _roots(tmp)
    sample = _sample_root(tmp)
    proc, calls, audit = _run(tree, "bookshelf", "extract", "--sample", CHECK_extract="no pass")
    assert proc.returncode == 0, proc.stderr
    assert _stages(calls) == ["stage extract_bookshelf"]
    env = _env(calls)
    assert _same(env["EPISTEME_RAW_ROOT"], sample / "01_raw")
    assert _same(env["EPISTEME_PROCESSED_ROOT"], sample / "02_processed")
    assert _same(env["EPISTEME_CORPUS_ROOT"], sample / "03_corpus")
    assert env["EPISTEME_DB_TARGET"] == "secondary"
    assert _same(env["EPISTEME_DATA_ROOT"], roots["EPISTEME_DATA_ROOT"])
    assert _same(env["EPISTEME_LOCK_DIR"], roots["EPISTEME_LOCK_DIR"])
    # The stage held the production heavy lock (the shared lock folder).
    assert any(c.startswith("lock heavy ") for c in calls)
    assert not (roots["EPISTEME_LOCK_DIR"] / "heavy.lock").exists()
    # The slice is built from the production raw folder, inside the sample root.
    prep = [c for c in calls if c.startswith("prepare ")]
    assert len(prep) == 1
    assert _same(_arg(prep[0], "--raw-root"), roots["EPISTEME_RAW_ROOT"])
    assert _same(_arg(prep[0], "--sample-root"), sample)
    assert _same(_arg(prep[0], "--processed-root"), roots["EPISTEME_PROCESSED_ROOT"])
    # The run bracket goes to the secondary database.
    assert audit[0] == "run_start secondary"
    assert [a.split()[1] for a in audit if a.startswith("run_end")] == ["secondary"]
    # The status lives under the sample root, not under production.
    assert _no_runs(tmp)
    assert list((sample / "02_processed" / "_ops" / "runs").glob("*/status.json"))
    # The run record of `gate run` exists.
    assert (sample / "run_record_extract.json").is_file()


def test_sample_writes_the_pass_under_the_production_processed_root(tree):
    tmp = tree[1]
    proc, calls, _ = _run(tree, "bookshelf", "extract", "--sample", CHECK_extract="no pass")
    assert proc.returncode == 0, proc.stderr
    report = [c for c in calls if c.startswith("report ")]
    assert len(report) == 1
    assert _same(_arg(report[0], "--pass-root"), _roots(tmp)["EPISTEME_PROCESSED_ROOT"])
    assert _same(_arg(report[0], "--sample-root"), _sample_root(tmp))
    assert report[0].startswith("report secondary ")
    passes = _roots(tmp)["EPISTEME_PROCESSED_ROOT"] / "_ops" / "bookshelf" / "gate"
    assert (passes / "extract.pass").is_file()
    assert "gate PASS for bookshelf extract" in proc.stdout


def test_a_failed_sample_report_exits_1_with_the_failed_rules(tree):
    proc, _, audit = _run(
        tree, "bookshelf", "extract", "--sample", CHECK_extract="no pass", RC_REPORT="1"
    )
    assert proc.returncode == 1, proc.stderr
    assert "min_rows: rows is 0" in proc.stdout
    assert len([a for a in audit if a.startswith("run_end")]) == 1


def test_sample_extract_starts_from_an_empty_sample_output(tree):
    """Old sample staging and markers would let the run skip every file."""
    sample = _sample_root(tree[1])
    old = [
        sample / "02_processed" / "staging" / "bookshelf" / "old.parquet",
        sample / "02_processed" / "_ops" / "bookshelf" / "success" / "a.ok",
        sample / "03_corpus" / "x.jsonl",
    ]
    for p in old:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    proc, _, _ = _run(tree, "bookshelf", "extract", "--sample", CHECK_extract="no pass")
    assert proc.returncode == 0, proc.stderr
    assert not any(p.exists() for p in old)


def test_a_failed_prepare_refuses_with_2_and_no_audit_row(tree):
    proc, calls, audit = _run(
        tree, "bookshelf", "extract", "--sample", CHECK_extract="no pass", RC_PREPARE="2"
    )
    assert proc.returncode == 2, proc.stderr
    assert _stages(calls) == [] and audit == []
    assert not (_roots(tree[1])["EPISTEME_LOCK_DIR"] / "heavy.lock").exists()


def test_sample_waits_for_a_live_foreign_heavy_lock_in_the_shared_folder(tree):
    locks = _roots(tree[1])["EPISTEME_LOCK_DIR"]
    locks.mkdir(parents=True)
    (locks / "heavy.lock").write_text(_foreign_lock(), encoding="utf-8")
    seen = []

    def free_it():
        for _ in range(600):
            if (locks / "heavy.request").exists():
                seen.append("request")
                break
            time.sleep(0.2)
        (locks / "heavy.lock").unlink(missing_ok=True)

    t = threading.Thread(target=free_it)
    t.start()
    proc, calls, _ = _run(tree, "bookshelf", "extract", "--sample", CHECK_extract="no pass")
    t.join()
    assert seen == ["request"]
    assert proc.returncode == 0, proc.stderr
    assert _stages(calls) == ["stage extract_bookshelf"]


def test_sample_with_no_wait_refuses_a_busy_heavy_lock_with_75(tree):
    locks = _roots(tree[1])["EPISTEME_LOCK_DIR"]
    locks.mkdir(parents=True)
    foreign = _foreign_lock()
    (locks / "heavy.lock").write_text(foreign, encoding="utf-8")
    proc, calls, audit = _run(
        tree, "bookshelf", "extract", "--sample", "--no-wait", CHECK_extract="no pass"
    )
    assert proc.returncode == 75, proc.stderr
    assert audit == [] and _stages(calls) == []
    assert not any(c.startswith("prepare ") for c in calls)
    assert (locks / "heavy.lock").read_text(encoding="utf-8") == foreign


def _sample_staging(tmp: Path) -> None:
    staging = _sample_root(tmp) / "02_processed" / "staging" / "bookshelf"
    staging.mkdir(parents=True)
    (staging / "part-0.parquet").write_bytes(b"x")


def test_sample_load_without_sample_staging_refuses_with_2(tree):
    proc, calls, audit = _run(tree, "bookshelf", "load", "--sample", CHECK_load="no pass")
    assert proc.returncode == 2, proc.stderr
    assert "run the sample extract first" in proc.stderr
    assert _stages(calls) == [] and audit == []
    assert not any(c.startswith("migcheck") for c in calls)


def test_sample_load_with_a_missing_migration_refuses_with_2_and_names_it(tree):
    _sample_staging(tree[1])
    proc, calls, audit = _run(
        tree,
        "bookshelf",
        "load",
        "--sample",
        CHECK_load="no pass",
        MISSING_MIG="0009_apollo_pubchem_partitions.sql",
    )
    assert proc.returncode == 2, proc.stderr
    assert "0009_apollo_pubchem_partitions.sql" in proc.stderr
    assert "migcheck secondary" in calls
    assert _stages(calls) == [] and audit == []


def test_sample_load_runs_on_the_sample_staging(tree):
    tmp = tree[1]
    _sample_staging(tmp)
    proc, calls, audit = _run(tree, "bookshelf", "load", "--sample", CHECK_load="no pass")
    assert proc.returncode == 0, proc.stderr
    assert "migcheck secondary" in calls
    assert not any(c.startswith("prepare ") for c in calls)
    assert _stages(calls) == ["stage load_bookshelf"]
    assert (_sample_root(tmp) / "02_processed" / "staging" / "bookshelf").is_dir()
    assert audit[0] == "run_start secondary"
    assert (
        _roots(tmp)["EPISTEME_PROCESSED_ROOT"] / "_ops" / "bookshelf" / "gate" / "load.pass"
    ).is_file()


@pytest.mark.pg
def test_a_sample_load_against_episteme_test_checks_the_real_migrations(tree):
    """The real `gate migrations-check` on episteme_test (local machine only).
    On 2026-10-21 episteme_test had the base schema only, so this refuses."""
    tmp = tree[1]
    _sample_staging(tmp)
    fake = (tmp / "fakepy").read_text(encoding="utf-8")
    _write(
        tmp / "fakepy",
        fake.replace(
            '*"episteme.ops.gate migrations-check"*)',
            '*"episteme.ops.gate migrations-check-fake"*)',
        ),
    )
    keep = {k: v for k, v in os.environ.items() if k.startswith("PG") and k != "PGDATABASE"}
    if not keep.get("PGPASSWORD") and not keep.get("PGHOST"):
        pytest.skip("source .env first: no PG connection settings in the environment")
    proc, calls, audit = _run(
        tree,
        "bookshelf",
        "load",
        "--sample",
        CHECK_load="no pass",
        **keep,
        PGDATABASE="episteme_spawn_blocked",
        PGDATABASE_SECONDARY="episteme_test",
    )
    if proc.returncode == 2:
        assert "missing" in proc.stderr, proc.stderr
        assert _stages(calls) == [] and audit == []
    else:
        assert proc.returncode == 0, proc.stderr
