"""End to end: a TERM to the runner bash ends the stage and its pool workers.

2026-10-26: ``kill -TERM`` to ``run_pipeline.sh`` did nothing while a stage
ran in the foreground (bash defers a trap until the foreground child exits),
and on Windows bash cannot signal a native Python. The runner now starts the
stage in the background, writes the stop-request file on INT/TERM, and waits
for the stage before it exits.

The harness sources the same code as ``run_pipeline.sh``
(``scripts/data/_lib/stage_run.sh``): ``stage_install_stop_traps`` and
``stage_exec``. A full ``run_pipeline.sh`` run needs a wired source (its
dispatch table names the stage scripts), the lock, the status file and the
audit bracket, so this test drives the extracted run/trap code instead.

The stand-in stage runs a 2-worker process pool of long-sleeping units under
``stage_guard``. Nothing touches a database (``spawn_child_env``). The test
kills only processes it started.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "scripts" / "data" / "_lib"

STAGE = """\
import os
import sys
import time
from pathlib import Path

from episteme.data.parallel import run_per_file
from episteme.ops.stop import stage_guard


def unit(path, *, raw_dir, pids):
    (pids / str(os.getpid())).write_text("", encoding="utf-8")
    time.sleep(300)
    return {"source_file": path.name, "skipped": False, "ok": True}


def main(out):
    raw = out / "raw"
    raw.mkdir()
    files = []
    for name in ("a.txt", "b.txt"):
        (raw / name).write_text(name, encoding="utf-8")
        files.append(raw / name)
    pids = out / "pids"
    pids.mkdir()
    (out / "stage.pid").write_text(str(os.getpid()), encoding="utf-8")
    with stage_guard(lambda: None, argv=[]):
        run_per_file(unit, files, workers=2, raw_dir=raw, pids=pids)


if __name__ == "__main__":
    out = Path(sys.argv[1])
    try:
        main(out)
    except SystemExit as exc:
        (out / "stage.rc").write_text(str(exc.code), encoding="utf-8")
        raise
"""

# After the stop, run_pipeline.sh's EXIT handler can start more Python under
# stage_guard (the --bulk index rebuild). The stop request must not stop it.
AFTER = """\
import sys
import time
from pathlib import Path

from episteme.ops.stop import stage_guard

with stage_guard(lambda: None, argv=[]):
    for _ in range(30):
        time.sleep(0.1)
Path(sys.argv[1], "after.ok").write_text("ok", encoding="utf-8")
"""

# The runner: the same trap and run code as run_pipeline.sh.
RUNNER = """\
. "$SR_LIB/common.sh"
. "$SR_LIB/stage_run.sh"
export EPISTEME_STOP_FILE="$SR_STOP"
trap '"$SR_PY" "$SR_AFTER" "$SR_OUT"' EXIT
stage_install_stop_traps
echo "$$" > "$SR_OUT/runner.pid"
rc=0
stage_exec "$SR_PY" "$SR_STAGE" "$SR_OUT" || rc=$?
echo "$rc" > "$SR_OUT/runner.rc"
exit "$rc"
"""


def _bash() -> Path | None:
    """On Windows Git's ``usr/bin/bash.exe`` (an MSYS process, not WSL's);
    elsewhere the ``bash`` on PATH."""
    if sys.platform != "win32":
        found = shutil.which("bash")
        return Path(found) if found else None
    candidates = [Path(r"C:\Program Files\Git\usr\bin\bash.exe")]
    git = shutil.which("git")
    if git:
        candidates.append(Path(git).resolve().parents[1] / "usr" / "bin" / "bash.exe")
    return next((c for c in candidates if c.is_file()), None)


def _posix(p: Path) -> str:
    return str(p).replace("\\", "/")


def _ours(pid: int, t_start: float) -> psutil.Process | None:
    """A handle on ``pid`` only when it started after this test did."""
    try:
        p = psutil.Process(pid)
        return p if p.create_time() >= t_start - 1 else None
    except psutil.NoSuchProcess:
        return None


def _running(p: psutil.Process) -> bool:
    try:
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def test_a_term_to_the_runner_ends_the_stage_and_its_workers(tmp_path, spawn_child_env):
    bash = _bash()
    if bash is None:
        pytest.skip("needs bash")
    out = tmp_path / "out"
    out.mkdir()
    stage = tmp_path / "stage.py"
    stage.write_text(STAGE, encoding="utf-8")
    after = tmp_path / "after.py"
    after.write_text(AFTER, encoding="utf-8")
    stop_file = tmp_path / "run" / "stop.request"  # the folder does not exist yet

    env = {k: v for k, v in os.environ.items() if k not in ("EPISTEME_RUNNER_PID",)}
    env.update(
        SR_LIB=_posix(LIB),
        SR_PY=_posix(Path(sys.executable)),
        SR_STAGE=_posix(stage),
        SR_AFTER=_posix(after),
        SR_OUT=_posix(out),
        SR_STOP=_posix(stop_file),
    )
    spawned: list[psutil.Process] = []
    t_start = time.time()
    runner = subprocess.Popen(
        [str(bash), "-c", RUNNER],
        cwd=tmp_path,  # no pyproject.toml here: get_settings() loads no .env
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        spawned.append(psutil.Process(runner.pid))
        pids = out / "pids"
        deadline = time.monotonic() + 90
        while not (pids.is_dir() and len(list(pids.iterdir())) >= 2):
            assert time.monotonic() < deadline, "the workers did not start"
            assert runner.poll() is None, f"the runner exited early (rc={runner.returncode})"
            time.sleep(0.2)
        stage_proc = _ours(int((out / "stage.pid").read_text()), t_start)
        workers = [_ours(int(p.name), t_start) for p in pids.iterdir()]
        assert stage_proc is not None and all(w is not None for w in workers)
        spawned += [stage_proc, *workers]
        assert stop_file.exists() is False  # removed before the stage, not yet written

        runner_pid = (out / "runner.pid").read_text().strip()
        subprocess.run([str(bash), "-c", f"kill -TERM {runner_pid}"], check=True, timeout=30)
        t0 = time.monotonic()
        rc = runner.wait(timeout=30)
        elapsed = time.monotonic() - t0
        err = runner.stderr.read().decode(errors="replace") if runner.stderr else ""

        assert rc == 143, f"runner exit code {rc}; stderr:\n{err}"
        assert elapsed < 30
        # The runner waited for the stage: nothing is left when it has exited.
        left = [p.pid for p in [stage_proc, *workers] if _running(p)]
        assert left == [], f"still running after the runner exited: {left}"
        # The stop came through the stop file (the stage exited 143 itself).
        assert stop_file.read_text(encoding="utf-8").strip() == "TERM"
        assert (out / "stage.rc").read_text().strip() == "143"
        assert not (out / "runner.rc").exists()  # the trap exits; the script does not go on
        # The EXIT handler's Python ran to its end: the request was for the stage only.
        assert (out / "after.ok").read_text(encoding="utf-8") == "ok", err
    finally:
        for p in reversed(spawned):
            try:
                if p.is_running():
                    p.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(spawned, timeout=5)
        if runner.poll() is None:
            runner.kill()
            runner.wait(5)
