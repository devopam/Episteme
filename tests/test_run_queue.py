"""run_queue.sh: ordered steps, stops at the first step that did not finish.

No DB, no network. A copy of scripts/data in a temp git repo with a fake
run_pipeline.sh. Every root is a temp path (controller ruling R2)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_run_pipeline_bulk import BASH, REPO_ROOT, _write

REAL_PY = Path(sys.executable).as_posix()

FAKE_PY = """#!/usr/bin/env bash
case "$*" in
    *"-m episteme.ops."*) exec "$REAL_PY" "$@" ;;
esac
exit 0
"""

# Logs the step; prints the done line unless NODONE names the step; WRONGDONE
# prints another step's line; exits $RC_<src>_<stage>; TOUCH_AT "<src> <stage>"
# edits TOUCH_FILE (a path under scripts/data; default a tracked file).
FAKE_PIPELINE = """#!/usr/bin/env bash
echo "step $1 $2" >> "$CALLS"
if [ "${TOUCH_AT:-}" = "$1 $2" ]; then
    echo changed >> "$(dirname "$0")/${TOUCH_FILE:-_lib/common.sh}"
fi
rc_var="RC_${1}_${2}"
if [ -n "${WRONGDONE:-}" ]; then
    echo "2026-01-01T00:00:00Z [INFO] pipeline done: $WRONGDONE" >&2
elif [ "${NODONE:-}" != "$1 $2" ]; then
    echo "2026-01-01T00:00:00Z [INFO] pipeline done: $1 $2" >&2
fi
exit "${!rc_var:-0}"
"""


@pytest.fixture
def tree(tmp_path):
    repo = tmp_path / "tree"
    data = repo / "scripts" / "data"
    shutil.copytree(REPO_ROOT / "scripts" / "data", data)
    _write(data / "run_pipeline.sh", FAKE_PIPELINE)
    _write(tmp_path / "fakepy", FAKE_PY)
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run(
        [*git, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"],
        check=True,
    )
    return repo, tmp_path


def _run(tree, *args: str, **env_over: str):
    repo, tmp = tree
    env = {k: v for k, v in os.environ.items() if not k.startswith(("EPISTEME_", "PG"))}
    env.update(
        PYTHON=(tmp / "fakepy").as_posix(),
        REAL_PY=REAL_PY,
        CALLS=(tmp / "calls.log").as_posix(),
        EPISTEME_ACTOR="pytest",
        EPISTEME_DATA_ROOT=(tmp / "data").as_posix(),
        EPISTEME_RAW_ROOT=(tmp / "data" / "01_raw").as_posix(),
        EPISTEME_PROCESSED_ROOT=(tmp / "data" / "02_processed").as_posix(),
        EPISTEME_LOCK_DIR=(tmp / "data" / "_ops" / "locks").as_posix(),
    )
    env.update(env_over)
    proc = subprocess.run(
        [BASH, str(repo / "scripts" / "data" / "run_queue.sh"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    calls = tmp / "calls.log"
    steps = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return proc, steps


def _qdir(tmp: Path) -> Path:
    dirs = list((tmp / "data" / "02_processed" / "_ops" / "queue").glob("queue-*"))
    assert len(dirs) == 1, dirs
    return dirs[0]


def test_all_steps_done(tree):
    proc, steps = _run(tree, "pubmed:load,graph")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert steps == ["step pubmed load", "step pubmed graph"]
    q = _qdir(tree[1])
    assert (q / "queue.log").is_file()
    assert sorted(p.name for p in q.glob("0*.log")) == ["01-pubmed-load.log", "02-pubmed-graph.log"]


def test_step_without_done_line_stops_the_queue(tree):
    proc, steps = _run(tree, "pubmed:load,graph", NODONE="pubmed load")
    assert proc.returncode == 1
    assert "STOPPED at pubmed load (rc=0)" in proc.stdout
    assert steps == ["step pubmed load"]


def test_done_line_must_match_exactly(tree):
    proc, steps = _run(tree, "pubmed:graph", WRONGDONE="pubmed load")
    assert proc.returncode == 1
    assert "STOPPED at pubmed graph" in proc.stdout
    assert steps == ["step pubmed graph"]


def test_failing_step_stops_with_rc(tree):
    proc, steps = _run(tree, "pubmed:load,graph", RC_pubmed_load="1")
    assert proc.returncode == 1
    assert "STOPPED at pubmed load (rc=1)" in proc.stdout
    assert steps == ["step pubmed load"]


def test_code_change_between_steps_stops(tree):
    proc, steps = _run(tree, "pubmed:load,graph", TOUCH_AT="pubmed load")
    assert proc.returncode == 1
    assert "STOPPED at pubmed graph: code changed since the queue started" in proc.stdout
    assert steps == ["step pubmed load"]


def test_edit_of_a_file_already_modified_at_start_stops(tree):
    """I2: the guard hashes content, not only the names of changed files."""
    repo, _ = tree
    with (repo / "scripts" / "data" / "_lib" / "common.sh").open("a", encoding="utf-8") as fh:
        fh.write("# edited before the queue\n")
    proc, steps = _run(tree, "pubmed:load,graph", TOUCH_AT="pubmed load")
    assert proc.returncode == 1
    assert "STOPPED at pubmed graph: code changed since the queue started" in proc.stdout
    assert steps == ["step pubmed load"]


def test_edit_of_an_untracked_file_already_there_stops(tree):
    repo, _ = tree
    (repo / "scripts" / "data" / "extra.sh").write_text("# untracked\n", encoding="utf-8")
    proc, steps = _run(tree, "pubmed:load,graph", TOUCH_AT="pubmed load", TOUCH_FILE="extra.sh")
    assert proc.returncode == 1
    assert "STOPPED at pubmed graph: code changed since the queue started" in proc.stdout
    assert steps == ["step pubmed load"]


def test_unchanged_dirty_tree_runs_every_step(tree):
    repo, _ = tree
    (repo / "scripts" / "data" / "extra.sh").write_text("# untracked\n", encoding="utf-8")
    with (repo / "scripts" / "data" / "_lib" / "common.sh").open("a", encoding="utf-8") as fh:
        fh.write("# edited before the queue\n")
    proc, steps = _run(tree, "pubmed:load,graph")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert steps == ["step pubmed load", "step pubmed graph"]


def test_no_git_runs_unguarded(tree):
    repo, _ = tree
    shutil.rmtree(repo / ".git", ignore_errors=True)
    proc, steps = _run(tree, "pubmed:load,graph", GIT_CEILING_DIRECTORIES=str(repo.parent))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.count("code-changed guard is off") == 1
    assert steps == ["step pubmed load", "step pubmed graph"]


@pytest.mark.parametrize("bad", ["pubmed", ":load", "pubmed:", "pubmed:load,"])
def test_bad_argument_runs_nothing(tree, bad):
    proc, steps = _run(tree, "pubmed:extract", bad)
    assert proc.returncode == 2
    assert steps == []
