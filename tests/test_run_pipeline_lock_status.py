"""run_pipeline.sh: the stage lock, the run status and exit 75 -- against fake stages.

No DB, no network. A copy of scripts/data with fake stage wrappers. The fake
PYTHON logs the audit calls (no DB) and hands every ``episteme.ops.*`` call
to the real interpreter, so the lock and status files are real. Every root
(data, raw, processed, lock folder) is a temp path (controller ruling R2)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import psutil
import pytest

from test_run_pipeline_bulk import BASH, REPO_ROOT, _write

REAL_PY = Path(sys.executable).as_posix()

# Audit calls go to $AUDIT; lock releases are logged to $CALLS; every other
# episteme.ops call runs for real; anything else (bulk_indexes, ...) is a no-op.
FAKE_PY = """#!/usr/bin/env bash
case "$*" in
    *audit_trail*run_start*) echo "run_start" >> "$AUDIT"; exit 0 ;;
    *audit_trail*run_end*) echo "run_end $*" >> "$AUDIT"; exit 0 ;;
    *audit_trail*) exit 0 ;;
    *"episteme.ops.lock release"*) echo "release $4" >> "$CALLS"; exec "$REAL_PY" "$@" ;;
    *"-m episteme.ops."*) exec "$REAL_PY" "$@" ;;
    *bulk_indexes*) echo "bulk_indexes $3" >> "$CALLS"; exit 0 ;;
esac
exit 0
"""

# Logs its name, the lock files and the run status it sees; can plant a lock
# of another run (FOREIGN_LOCK at stage FOREIGN_AT); exits with $RC_<name>.
FAKE_STAGE = """#!/usr/bin/env bash
name="$(basename "$0" .sh)"
echo "stage $name" >> "$CALLS"
[ -z "${EPISTEME_NO_WAIT:-}" ] || echo "no_wait $EPISTEME_NO_WAIT" >> "$CALLS"
for k in heavy download; do
    f="$EPISTEME_LOCK_DIR/$k.lock"
    [ -f "$f" ] && echo "lock $k $(tr -d '\\r\\n' < "$f")" >> "$CALLS"
done
if [ -n "${EPISTEME_RUN_DIR:-}" ] && [ -f "$EPISTEME_RUN_DIR/status.json" ]; then
    echo "status $(tr -d '\\r\\n' < "$EPISTEME_RUN_DIR/status.json")" >> "$CALLS"
fi
if [ -n "${FOREIGN_LOCK:-}" ] && [ "$name" = "${FOREIGN_AT:-}" ]; then
    printf '%s' "$FOREIGN_LOCK" > "$EPISTEME_LOCK_DIR/heavy.lock"
fi
# WAITKILL_SIG: once the runner waits for the heavy lock (the acquire posted
# heavy.request), log the status, signal the runner, then free the lock so
# the acquire child can exit (bash runs the trap after the child exits).
if [ -n "${WAITKILL_SIG:-}" ] && [ "$name" = "${FOREIGN_AT:-}" ]; then
    runner=$PPID
    (
        n=0
        while [ ! -f "$EPISTEME_LOCK_DIR/heavy.request" ] && [ "$n" -lt 600 ]; do
            sleep 0.2; n=$((n + 1))
        done
        echo "waitstatus $(tr -d '\\r\\n' < "$EPISTEME_RUN_DIR/status.json")" >> "$CALLS"
        kill -"$WAITKILL_SIG" "$runner"
        sleep 1
        rm -f "$EPISTEME_LOCK_DIR/heavy.lock"
    ) </dev/null >/dev/null 2>&1 &
fi
sig_var="SIG_${name}"
[ -z "${!sig_var:-}" ] || kill -"${!sig_var}" "$PPID"
rc_var="RC_${name}"
exit "${!rc_var:-0}"
"""

FAKE_BULK = """#!/usr/bin/env bash
echo "bulk $1" >> "$CALLS"
[ "$1" = drop ] && [ -n "${SIG_BULK_DROP:-}" ] && kill -"$SIG_BULK_DROP" "$PPID"
exit 0
"""

# Fails on its 2nd unit once (records the failure as a real stage does);
# skips the units that already have a done marker.
FAKE_RESUME = """#!/usr/bin/env bash
marks="$EPISTEME_PROCESSED_ROOT/markers"
mkdir -p "$marks"
for u in u1 u2 u3; do
    [ -f "$marks/$u" ] && continue
    if [ "$u" = u2 ] && [ ! -f "$marks/.failed_once" ]; then
        touch "$marks/.failed_once"
        "$REAL_PY" -c "from episteme.ops.failure import record_failure; \\
record_failure('pubmed', 'extract', 'u2', 'boom')"
        exit 1
    fi
    echo "unit $u" >> "$CALLS"
    touch "$marks/$u"
done
exit 0
"""

STAGES = (
    "pubmed/download_pubmed.sh",
    "pubmed/extract_pubmed.sh",
    "pubmed/load_pubmed.sh",
    "pubmed/graph_pubmed.sh",
)


@pytest.fixture
def tree(tmp_path):
    data = tmp_path / "tree" / "scripts" / "data"
    shutil.copytree(REPO_ROOT / "scripts" / "data", data)
    for rel in STAGES:
        _write(data / rel, FAKE_STAGE)
    fake_py = tmp_path / "fakepy"
    _write(fake_py, FAKE_PY)
    return data, tmp_path


def _foreign_lock() -> str:
    me = psutil.Process()
    return json.dumps(
        {
            "kind": "heavy",
            "pid": me.pid,
            "create_time": me.create_time(),
            "run_id": "other-run",
            "stage": "graph",
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "state": "running",
        }
    )


def _run(tree, stage: str, *args: str, script: str = "run_pipeline.sh", **env_over: str):
    data, tmp = tree
    env = {k: v for k, v in os.environ.items() if not k.startswith(("EPISTEME_", "PG"))}
    env.update(
        EPISTEME_ACTOR="pytest",
        PYTHON=(tmp / "fakepy").as_posix(),
        REAL_PY=REAL_PY,
        CALLS=(tmp / "calls.log").as_posix(),
        AUDIT=(tmp / "audit.log").as_posix(),
        EPISTEME_DATA_ROOT=(tmp / "data").as_posix(),
        EPISTEME_RAW_ROOT=(tmp / "data" / "01_raw").as_posix(),
        EPISTEME_PROCESSED_ROOT=(tmp / "data" / "02_processed").as_posix(),
        EPISTEME_LOCK_DIR=(tmp / "data" / "_ops" / "locks").as_posix(),
        EPISTEME_DB_MODE="restricted",
        PGDATABASE="episteme_spawn_blocked",
        EPISTEME_PRODUCTION_DATABASE="episteme_spawn_blocked",
    )
    env.update(env_over)
    argv = [BASH, str(data / script)]
    argv += ["pubmed", stage, *args] if script == "run_pipeline.sh" else [stage, *args]
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=300)
    return proc, _lines(tmp / "calls.log"), _lines(tmp / "audit.log")


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _locks(tmp: Path) -> Path:
    return tmp / "data" / "_ops" / "locks"


def _seen(calls: list[str], prefix: str) -> list[dict]:
    return [json.loads(c[len(prefix) :]) for c in calls if c.startswith(prefix)]


def _status(tmp: Path) -> dict:
    runs = list((tmp / "data" / "02_processed" / "_ops" / "runs").glob("*/status.json"))
    assert len(runs) == 1, runs
    return json.loads(runs[0].read_text(encoding="utf-8"))


def test_load_holds_the_heavy_lock_and_releases_it(tree):
    proc, calls, _ = _run(tree, "load")
    assert proc.returncode == 0, proc.stderr
    held = _seen(calls, "lock heavy ")
    assert len(held) == 1
    assert held[0]["run_id"].startswith("pubmed-") and held[0]["stage"] == "load"
    assert not _seen(calls, "lock download ")
    assert not (_locks(tree[1]) / "heavy.lock").exists()


def test_download_holds_the_download_lock(tree):
    proc, calls, _ = _run(tree, "download")
    assert proc.returncode == 0, proc.stderr
    held = _seen(calls, "lock download ")
    assert len(held) == 1 and held[0]["stage"] == "download"
    assert not _seen(calls, "lock heavy ")
    assert not (_locks(tree[1]) / "download.lock").exists()


def test_no_wait_refuses_a_busy_heavy_lock_with_75_and_no_audit_row(tree):
    locks = _locks(tree[1])
    locks.mkdir(parents=True)
    foreign = _foreign_lock()
    (locks / "heavy.lock").write_text(foreign, encoding="utf-8")
    proc, calls, audit = _run(tree, "load", "--no-wait")
    assert proc.returncode == 75, proc.stderr
    assert "refused:" in proc.stderr
    assert audit == []
    assert not any(c.startswith("stage ") for c in calls)
    st = _status(tree[1])
    assert st["state"] == "refused" and "other-run" in (st.get("reason") or "")
    assert (locks / "heavy.lock").read_text(encoding="utf-8") == foreign


def test_all_refused_at_its_second_stage_writes_one_refused_run_end(tree):
    proc, calls, audit = _run(
        tree, "all", "--no-wait", FOREIGN_LOCK=_foreign_lock(), FOREIGN_AT="download_pubmed"
    )
    assert proc.returncode == 75, proc.stderr
    assert [c for c in calls if c.startswith("stage ")] == ["stage download_pubmed"]
    assert audit[0] == "run_start"
    ends = [a for a in audit if a.startswith("run_end")]
    assert len(ends) == 1
    assert "refused: heavy lock held by other-run (graph)" in ends[0]
    st = _status(tree[1])
    assert st["state"] == "refused" and st["stage"] == "extract"
    assert not (_locks(tree[1]) / "download.lock").exists()


def test_all_takes_and_releases_the_lock_per_stage(tree):
    proc, calls, _ = _run(tree, "all")
    assert proc.returncode == 0, proc.stderr
    assert [c for c in calls if c.startswith("release ")] == [
        "release download",
        "release heavy",
        "release heavy",
        "release heavy",
    ]
    stages = [h["stage"] for h in _seen(calls, "lock heavy ")]
    assert stages == ["extract", "load", "graph"]


def test_status_is_running_during_the_stage_and_done_after(tree):
    proc, calls, _ = _run(tree, "load")
    assert proc.returncode == 0, proc.stderr
    during = _seen(calls, "status ")
    assert len(during) == 1
    assert during[0]["state"] == "running" and during[0]["stage"] == "load"
    assert during[0]["pids"]["runner"]["pid"] > 0
    after = _status(tree[1])
    assert after["state"] == "done" and after["stage"] == "load"


def test_a_stage_that_exits_143_leaves_stopped(tree):
    proc, _, _ = _run(tree, "load", RC_load_pubmed="143")
    assert proc.returncode == 143, proc.stderr
    assert _status(tree[1])["state"] == "stopped"
    assert not (_locks(tree[1]) / "heavy.lock").exists()


def test_a_failed_stage_leaves_failed(tree):
    proc, _, _ = _run(tree, "load", RC_load_pubmed="1")
    assert proc.returncode == 1, proc.stderr
    assert _status(tree[1])["state"] == "failed"


@pytest.mark.parametrize(("sig", "rc"), [("INT", 130), ("TERM", 143)])
def test_a_signal_to_the_runner_writes_one_stopped_run_end(tree, sig, rc):
    """The INT/TERM trap fires before run_stage's own run_end; _on_exit writes it."""
    proc, _, audit = _run(tree, "load", SIG_load_pubmed=sig, RC_load_pubmed=str(rc))
    assert proc.returncode == rc, proc.stderr
    ends = [a for a in audit if a.startswith("run_end")]
    assert len(ends) == 1, audit
    assert "stopped at stage load" in ends[0]
    assert _status(tree[1])["state"] == "stopped"
    assert not (_locks(tree[1]) / "heavy.lock").exists()


FAKE_FAIL_UNIT = """#!/usr/bin/env bash
"$REAL_PY" -c "import os; from episteme.ops.failure import record_failure; \\
record_failure('pubmed', 'load', os.environ['UNIT'], 'boom: x')"
exit 1
"""


@pytest.mark.parametrize("unit", ["a: b.xml", ""])
def test_the_failed_unit_reaches_the_status_whole(tree, unit):
    _write(tree[0] / "pubmed" / "load_pubmed.sh", FAKE_FAIL_UNIT)
    proc, _, audit = _run(tree, "load", UNIT=unit)
    assert proc.returncode == 1, proc.stderr
    st = _status(tree[1])
    assert st["state"] == "failed"
    assert st.get("unit") == (unit or None)
    ends = [a for a in audit if a.startswith("run_end")]
    assert len(ends) == 1 and f"failed at stage load: {unit}: boom: x" in ends[0]


def test_a_refusal_without_a_holder_in_the_message_says_lock_busy(tree):
    tmp = tree[1]
    _write(
        tmp / "fakepy",
        FAKE_PY.replace(
            '*"episteme.ops.lock release"*)',
            '*"episteme.ops.lock acquire"*) echo "busy: odd" >&2; exit 75 ;;\n'
            '    *"episteme.ops.lock release"*)',
        ),
    )
    proc, _, audit = _run(tree, "load", "--no-wait")
    assert proc.returncode == 75, proc.stderr
    assert "refused: heavy lock busy" in proc.stderr
    assert audit == []
    assert _status(tmp)["reason"] == "refused: heavy lock busy"


def test_a_stage_that_exits_75_passes_through(tree):
    proc, _, audit = _run(tree, "load", RC_load_pubmed="75")
    assert proc.returncode == 75, proc.stderr
    assert _status(tree[1])["state"] == "refused"
    ends = [a for a in audit if a.startswith("run_end")]
    assert len(ends) == 1 and "refused at stage load" in ends[0]


def test_bulk_with_a_failing_stage_rebuilds_then_releases(tree):
    _write(tree[0] / "db" / "bulk_load_indexes.sh", FAKE_BULK)
    proc, calls, _ = _run(tree, "load", "--bulk", RC_load_pubmed="1")
    assert proc.returncode == 1, proc.stderr
    tail = [c for c in calls if c in ("bulk rebuild", "release heavy")]
    assert tail == ["bulk rebuild", "release heavy"]
    assert not (_locks(tree[1]) / "heavy.lock").exists()


def test_bulk_load_indexes_under_the_runner_reenters_and_keeps_the_lock(tree):
    proc, calls, _ = _run(tree, "load", "--bulk", EPISTEME_SYS_ADMIN_PASSWORD="x")
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.count("heavy lock: reentrant") == 2  # drop and rebuild
    assert "heavy lock: acquired" not in proc.stderr
    assert calls.index("bulk_indexes drop") < calls.index("stage load_pubmed")
    assert len(_seen(calls, "lock heavy ")) == 1  # the drop did not delete it
    assert [c for c in calls if c.startswith("release ")] == ["release heavy"]
    assert not (_locks(tree[1]) / "heavy.lock").exists()


def test_bulk_load_indexes_alone_takes_and_releases_the_lock(tree):
    proc, calls, _ = _run(
        tree, "drop", script="db/bulk_load_indexes.sh", EPISTEME_SYS_ADMIN_PASSWORD="x"
    )
    assert proc.returncode == 0, proc.stderr
    assert "heavy lock: acquired" in proc.stderr
    assert calls == ["bulk_indexes drop", "release heavy"]
    assert not (_locks(tree[1]) / "heavy.lock").exists()


def test_bulk_load_indexes_status_takes_no_lock(tree):
    proc, calls, _ = _run(
        tree, "status", script="db/bulk_load_indexes.sh", EPISTEME_SYS_ADMIN_PASSWORD="x"
    )
    assert proc.returncode == 0, proc.stderr
    assert "heavy lock" not in proc.stderr
    assert calls == ["bulk_indexes status"]


FAKE_PSQL = """#!/usr/bin/env bash
f="$EPISTEME_LOCK_DIR/heavy.lock"
[ -f "$f" ] && echo "lock heavy $(tr -d '\\r\\n' < "$f")" >> "$CALLS"
exit 0
"""


def test_migrate_database_takes_and_releases_the_heavy_lock(tree):
    data, tmp = tree
    (data.parents[1] / "src" / "episteme" / "data" / "db" / "migrations").mkdir(parents=True)
    _write(tmp / "fakepsql", FAKE_PSQL)
    proc, calls, _ = _run(
        tree,
        "episteme_test",
        script="db/migrate_database.sh",
        PSQL=(tmp / "fakepsql").as_posix(),
        PGHOST="127.0.0.1",
        PGPORT="9",
        EPISTEME_SYS_ADMIN_PASSWORD="x",
    )
    assert proc.returncode == 0, proc.stderr
    held = _seen(calls, "lock heavy ")
    assert held and all(h["stage"] == "migrate_database" for h in held)
    assert held[0]["run_id"].startswith("adhoc-")
    assert calls[-1] == "release heavy"
    assert not (_locks(tmp) / "heavy.lock").exists()


def test_migrate_database_passes_75_through(tree):
    data, tmp = tree
    (data.parents[1] / "src" / "episteme" / "data" / "db" / "migrations").mkdir(parents=True)
    _write(tmp / "fakepsql", FAKE_PSQL)
    locks = _locks(tmp)
    locks.mkdir(parents=True)
    (locks / "heavy.lock").write_text(_foreign_lock(), encoding="utf-8")
    # This proves the exit-75 pass-through only, not the real wait: the script
    # waits by default, so a fake lock module answers busy at once.
    _write(
        tmp / "fakepy",
        FAKE_PY.replace(
            '*"episteme.ops.lock release"*)',
            '*"episteme.ops.lock acquire"*) echo "busy: held" >&2; exit 75 ;;\n'
            '    *"episteme.ops.lock release"*)',
        ),
    )
    proc, calls, _ = _run(
        tree,
        "episteme_test",
        script="db/migrate_database.sh",
        PSQL=(tmp / "fakepsql").as_posix(),
        PGHOST="127.0.0.1",
        PGPORT="9",
        EPISTEME_SYS_ADMIN_PASSWORD="x",
    )
    assert proc.returncode == 75, proc.stderr
    assert calls == []  # no psql call, no release


def test_dry_run_takes_no_lock_and_writes_no_status(tree):
    proc, calls, audit = _run(tree, "download", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert calls == ["stage download_pubmed"]
    assert audit == []
    assert not (tree[1] / "data" / "02_processed" / "_ops" / "runs").exists()


def test_a_rerun_resumes_at_the_failed_unit(tree):
    """Spec section 8 (drift log backlog (j))."""
    _write(tree[0] / "pubmed" / "extract_pubmed.sh", FAKE_RESUME)
    proc, calls, audit = _run(tree, "extract")
    assert proc.returncode == 1, proc.stderr
    assert [c for c in calls if c.startswith("unit ")] == ["unit u1"]
    ends = [a for a in audit if a.startswith("run_end")]
    assert len(ends) == 1 and "failed at stage extract: u2: boom" in ends[0]
    st = _status(tree[1])
    assert st["state"] == "failed" and st["unit"] == "u2"
    assert "resume with:" in proc.stderr and "pubmed extract" in proc.stderr

    (tree[1] / "calls.log").unlink()
    proc, calls, _ = _run(tree, "extract")
    assert proc.returncode == 0, proc.stderr
    assert [c for c in calls if c.startswith("unit ")] == ["unit u2", "unit u3"]


@pytest.mark.parametrize(("sig", "rc"), [("TERM", 143), ("INT", 130)])
def test_a_signal_during_the_lock_wait_of_all_closes_the_bracket(tree, sig, rc):
    """I1: stage 2 of `all` waits for a live foreign heavy.lock; a signal then
    ends the run with exactly one run_end and leaves no lock of this run."""
    proc, calls, audit = _run(
        tree,
        "all",
        FOREIGN_LOCK=_foreign_lock(),
        FOREIGN_AT="download_pubmed",
        WAITKILL_SIG=sig,
    )
    assert proc.returncode == rc, proc.stderr
    assert [c for c in calls if c.startswith("stage ")] == ["stage download_pubmed"]
    waiting = _seen(calls, "waitstatus ")
    assert len(waiting) == 1
    assert waiting[0]["state"] == "running" and waiting[0]["stage"] == "extract"
    assert waiting[0]["unit"] == "waiting for the heavy lock"
    ends = [a for a in audit if a.startswith("run_end")]
    assert len(ends) == 1, audit
    assert "stopped while waiting for the heavy lock (stage extract)" in ends[0]
    st = _status(tree[1])
    assert st["state"] == "stopped" and st["stage"] == "extract"
    locks = _locks(tree[1])
    assert not (locks / "heavy.lock").exists()
    assert not (locks / "download.lock").exists()
    assert not (locks / "heavy.request").exists()


def test_a_signal_during_the_bulk_drop_closes_the_bracket(tree):
    _write(tree[0] / "db" / "bulk_load_indexes.sh", FAKE_BULK)
    proc, calls, audit = _run(tree, "load", "--bulk", SIG_BULK_DROP="TERM")
    assert proc.returncode == 143, proc.stderr
    assert "stage load_pubmed" not in calls
    assert [c for c in calls if c.startswith("bulk ")] == ["bulk drop", "bulk rebuild"]
    ends = [a for a in audit if a.startswith("run_end")]
    assert len(ends) == 1, audit
    assert "stopped at stage load (bulk index drop)" in ends[0]
    assert _status(tree[1])["state"] == "stopped"
    assert not (_locks(tree[1]) / "heavy.lock").exists()


def test_a_single_stage_shows_the_wait_then_running(tree):
    """While a single stage waits for its lock the status says so; once held it
    no longer shows the wait."""
    proc, calls, _ = _run(tree, "load")
    assert proc.returncode == 0, proc.stderr
    during = _seen(calls, "status ")
    assert len(during) == 1 and during[0].get("unit") is None


def test_no_wait_reaches_the_python_stages(tree):
    """M1: --no-wait exports EPISTEME_NO_WAIT=1 (the advisory lock refuses too)."""
    proc, calls, _ = _run(tree, "load", "--no-wait")
    assert proc.returncode == 0, proc.stderr
    assert "no_wait 1" in calls
    (tree[1] / "calls.log").unlink()
    proc, calls, _ = _run(tree, "graph")
    assert proc.returncode == 0, proc.stderr
    assert not any(c.startswith("no_wait") for c in calls)
