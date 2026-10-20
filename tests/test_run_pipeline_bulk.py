"""run_pipeline.sh --bulk: drop, load, always rebuild -- against fake scripts.

No DB, no network. A copy of scripts/data with fake load_pubmed.sh,
bulk_load_indexes.sh and a fake PYTHON (the audit calls) records the call
order. The copy sits under tmp_path, with no pyproject.toml above it, so
load_dotenv finds no .env."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _find_bash() -> str:
    """A real POSIX bash -- never Windows' WSL shim in System32/WindowsApps."""
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
    pytest.skip("no usable POSIX bash", allow_module_level=True)

FAKE_LOAD = """#!/usr/bin/env bash
echo "load" >> "$CALLS"
case "${FAKE_LOAD:-ok}" in
    ok) exit 0 ;;
    fail) exit 1 ;;
    rc130) exit 130 ;;
    rc*) exit "${FAKE_LOAD#rc}" ;;
    runner) echo "runner ${EPISTEME_RUNNER_PID:-unset}" >> "$CALLS"; exit 0 ;;
    term) kill -TERM "$PPID"; sleep 1; exit 0 ;;
esac
"""
FAKE_BULK = """#!/usr/bin/env bash
echo "bulk $1" >> "$CALLS"
case "$1" in
    drop) exit "${FAKE_DROP_RC:-0}" ;;
    rebuild) exit "${FAKE_REBUILD_RC:-0}" ;;
    *) exit 0 ;;
esac
"""
FAKE_PY = """#!/usr/bin/env bash
case "$*" in
    *run_start*) echo "run_start" >> "$CALLS" ;;
    *run_end*) echo "run_end" >> "$CALLS" ;;
esac
exit 0
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


@pytest.fixture
def tree(tmp_path):
    data = tmp_path / "tree" / "scripts" / "data"
    shutil.copytree(REPO_ROOT / "scripts" / "data", data)
    _write(data / "pubmed" / "load_pubmed.sh", FAKE_LOAD)
    _write(data / "db" / "bulk_load_indexes.sh", FAKE_BULK)
    fake_py = tmp_path / "fakepy"
    _write(fake_py, FAKE_PY)
    return data / "run_pipeline.sh", tmp_path / "calls.log", fake_py


def _run(tree, stage: str, *args: str, **env_over: str):
    script, calls, fake_py = tree
    env = {k: v for k, v in os.environ.items() if not k.startswith(("EPISTEME_", "PG"))}
    env.update(
        EPISTEME_ACTOR="pytest",
        PYTHON=fake_py.as_posix(),
        CALLS=calls.as_posix(),
        EPISTEME_DB_MODE="restricted",
        PGDATABASE="episteme_spawn_blocked",
        EPISTEME_PRODUCTION_DATABASE="episteme_spawn_blocked",
        # Ruling R2: temp roots, though the fake PYTHON never runs real Python.
        EPISTEME_DATA_ROOT=(calls.parent / "data").as_posix(),
        EPISTEME_RAW_ROOT=(calls.parent / "data" / "01_raw").as_posix(),
        EPISTEME_PROCESSED_ROOT=(calls.parent / "data" / "02_processed").as_posix(),
        EPISTEME_LOCK_DIR=(calls.parent / "data" / "_ops" / "locks").as_posix(),
    )
    env.update(env_over)
    proc = subprocess.run(
        [BASH, str(script), "pubmed", stage, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return proc, lines


def test_bulk_success_rebuilds_before_run_end(tree):
    proc, calls = _run(tree, "load", "--bulk")
    assert proc.returncode == 0, proc.stderr
    assert calls == ["run_start", "bulk drop", "load", "bulk rebuild", "run_end"]


def test_bulk_rebuilds_after_a_failed_load_and_keeps_its_exit_code(tree):
    proc, calls = _run(tree, "load", "--bulk", FAKE_LOAD="fail")
    assert proc.returncode == 1, proc.stderr
    assert calls == ["run_start", "bulk drop", "load", "run_end", "bulk rebuild"]


def test_bulk_rebuilds_after_an_interrupted_load(tree):
    proc, calls = _run(tree, "load", "--bulk", FAKE_LOAD="term")
    assert proc.returncode == 143, proc.stderr
    assert calls.count("bulk rebuild") == 1
    assert calls[-1] == "bulk rebuild"
    assert "load" in calls


def test_bulk_rebuild_failure_is_the_exit_code_and_runs_once(tree):
    proc, calls = _run(tree, "load", "--bulk", FAKE_REBUILD_RC="1")
    assert proc.returncode == 1, proc.stderr
    assert calls == ["run_start", "bulk drop", "load", "bulk rebuild", "run_end"]


def test_bulk_trap_rebuild_failure_is_logged_and_keeps_the_load_exit_code(tree):
    proc, calls = _run(tree, "load", "--bulk", FAKE_LOAD="fail", FAKE_REBUILD_RC="1")
    assert proc.returncode == 1, proc.stderr
    assert calls == ["run_start", "bulk drop", "load", "run_end", "bulk rebuild"]
    assert "index rebuild failed" in proc.stderr


def test_bulk_drop_failure_never_starts_the_load(tree):
    proc, calls = _run(tree, "load", "--bulk", FAKE_DROP_RC="2")
    assert proc.returncode == 2, proc.stderr
    assert calls == ["run_start", "bulk drop", "run_end"]


def test_load_without_bulk_touches_no_index(tree):
    proc, calls = _run(tree, "load")
    assert proc.returncode == 0, proc.stderr
    assert calls == ["run_start", "load", "run_end"]


def test_bulk_on_another_stage_is_a_usage_error(tree):
    proc, calls = _run(tree, "extract", "--bulk")
    assert proc.returncode == 2
    assert "--bulk is only valid with the load or graph stage" in proc.stderr
    assert calls == []


def test_bulk_graph_passes_validation_and_uses_the_pubmed_graph_group(tree):
    data = tree[0].parent
    _write(data / "db" / "bulk_load_indexes.sh", FAKE_BULK.replace('$1"', '$*"'))
    _write(data / "pubmed" / "graph_pubmed.sh", FAKE_LOAD.replace('"load"', '"graph"'))
    proc, calls = _run(tree, "graph", "--bulk")
    assert proc.returncode == 0, proc.stderr
    assert "--bulk is only valid" not in proc.stderr
    assert calls == [
        "run_start",
        "bulk drop --group pubmed_graph",
        "graph",
        "bulk rebuild",
        "run_end",
    ]


FAKE_GRAPH = """#!/usr/bin/env bash
echo "graph $*" >> "$CALLS"
exit 0
"""


def test_graph_forwards_force_and_reason_only_when_forced(tree):
    script, calls, _ = tree
    _write(script.parent / "pubmed" / "graph_pubmed.sh", FAKE_GRAPH)
    proc, lines = _run(tree, "graph", "--force", "--reason", "R one")
    assert proc.returncode == 0, proc.stderr
    assert "graph --force --reason R one" in lines
    calls.unlink()
    proc, lines = _run(tree, "graph")
    assert proc.returncode == 0, proc.stderr
    assert "graph " in lines or "graph" in lines
    assert not any(ln.startswith("graph ") and "--force" in ln for ln in lines)


FAKE_PY_FAILURE = """#!/usr/bin/env bash
case "$*" in
    *failure*show*) echo "file_7.xml: boom"; exit 0 ;;
    *failure*clear*) echo "clear" >> "$CALLS"; exit 0 ;;
    *run_end*) echo "run_end $*" >> "$CALLS" ;;
esac
exit 0
"""


def test_failed_stage_puts_the_recorded_unit_in_the_run_end_reason(tree):
    _write(tree[2], FAKE_PY_FAILURE)
    proc, calls = _run(tree, "load", FAKE_LOAD="fail")
    assert proc.returncode == 1, proc.stderr
    assert calls[0] == "clear"
    end = [c for c in calls if c.startswith("run_end")]
    assert len(end) == 1
    assert "failed at stage load: file_7.xml: boom" in end[0]


def test_stopped_stage_is_reported_as_stopped_and_keeps_its_exit_code(tree):
    _write(tree[2], FAKE_PY_FAILURE)
    proc, calls = _run(tree, "load", FAKE_LOAD="rc130")
    assert proc.returncode == 130, proc.stderr
    end = [c for c in calls if c.startswith("run_end")]
    assert len(end) == 1
    assert "stopped at stage load" in end[0]
    assert "failed at stage" not in end[0]


@pytest.mark.parametrize("stage_rc", ["2", "3", "7"])
def test_any_other_stage_code_becomes_exit_1(tree, stage_rc):
    """Exit 2 and 3 belong to the dispatcher's own checks before run_start."""
    _write(tree[2], FAKE_PY_FAILURE)
    proc, calls = _run(tree, "load", FAKE_LOAD=f"rc{stage_rc}")
    assert proc.returncode == 1, proc.stderr
    end = [c for c in calls if c.startswith("run_end")]
    assert len(end) == 1 and "failed at stage load" in end[0]


def test_sigterm_code_from_a_stage_passes_through(tree):
    _write(tree[2], FAKE_PY_FAILURE)
    proc, calls = _run(tree, "load", FAKE_LOAD="rc143")
    assert proc.returncode == 143, proc.stderr
    end = [c for c in calls if c.startswith("run_end")]
    assert len(end) == 1 and "stopped at stage load" in end[0]


def test_stage_sees_the_runner_pid(tree):
    proc, calls = _run(tree, "load", FAKE_LOAD="runner")
    assert proc.returncode == 0, proc.stderr
    runner = [c for c in calls if c.startswith("runner ")]
    assert len(runner) == 1 and runner[0].split()[1].isdigit(), runner


def test_an_outer_runner_pid_is_kept(tree):
    proc, calls = _run(tree, "load", FAKE_LOAD="runner", EPISTEME_RUNNER_PID="4242")
    assert proc.returncode == 0, proc.stderr
    assert "runner 4242" in calls
