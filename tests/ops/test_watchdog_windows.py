"""Windows acceptance test: the parent watchdog fires when the runner is killed.

The real tree under Git Bash is: outer bash (the runner, which sources
scripts/data/_lib/common.sh) -> inner bash (``exec`` stub) -> venv launcher
(``.venv/Scripts/python.exe``) -> real interpreter. A hard kill of the outer
bash leaves the stub and the launcher alive, so only ``EPISTEME_RUNNER_PID``
lets the interpreter notice. The child opens no DB connection.

The test kills only processes it spawned, through its own handles.
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

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows process tree only")

REPO = Path(__file__).resolve().parents[2]
COMMON_SH = REPO / "scripts" / "data" / "_lib" / "common.sh"
VENV_PY = REPO / ".venv" / "Scripts" / "python.exe"

CHILD = """\
import os, sys, time
from episteme.ops.stop import start_parent_watchdog

out = sys.argv[1]
start_parent_watchdog(lambda: None, interval=1.0)
with open(out + ".started", "w") as fh:
    fh.write(str(os.getpid()))
time.sleep(25)
with open(out, "a") as fh:
    fh.write("survived\\n")
"""


def _msys_bash() -> Path | None:
    """Git's ``usr/bin/bash.exe`` (an MSYS process, not the ``bin/bash.exe``
    launcher and not WSL's ``System32/bash.exe``)."""
    candidates = [Path(r"C:\Program Files\Git\usr\bin\bash.exe")]
    git = shutil.which("git")
    if git:
        candidates.append(Path(git).resolve().parents[1] / "usr" / "bin" / "bash.exe")
    found = shutil.which("bash")
    if found:
        p = Path(found)
        if p.parent.name.lower() == "bin" and p.parent.parent.name.lower() == "usr":
            candidates.append(p)
    for c in candidates:
        if c.is_file():
            return c
    return None


def _posix(p: Path) -> str:
    return str(p).replace("\\", "/")


def test_watchdog_exits_143_when_the_runner_bash_is_killed(tmp_path):
    bash = _msys_bash()
    if bash is None or not VENV_PY.is_file():
        pytest.skip("needs Git Bash (usr/bin/bash.exe) and the venv launcher")
    child = tmp_path / "child.py"
    child.write_text(CHILD, encoding="utf-8")
    out = tmp_path / "out.txt"
    started = Path(str(out) + ".started")

    env = {k: v for k, v in os.environ.items() if k != "EPISTEME_RUNNER_PID"}
    env.update(
        WD_COMMON=_posix(COMMON_SH),
        WD_BASH=_posix(bash),
        WD_PY=_posix(VENV_PY),
        WD_CHILD=_posix(child),
        WD_OUT=_posix(out),
        PYTHONPATH=str(REPO / "src"),
    )
    # The trailing echo stops bash from exec-ing the last command itself.
    script = (
        '. "$WD_COMMON"; "$WD_BASH" -c \'exec "$WD_PY" "$WD_CHILD" "$WD_OUT"\'; echo outer-done'
    )
    spawned: list[psutil.Process] = []
    t_start = time.time()
    outer = subprocess.Popen(
        [str(bash), "-c", script],
        cwd=tmp_path,  # no pyproject.toml here: get_settings() loads no .env
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        spawned.append(psutil.Process(outer.pid))
        deadline = time.monotonic() + 60
        while not started.is_file() or not started.read_text().strip():
            assert time.monotonic() < deadline, "the child did not start"
            assert outer.poll() is None, f"outer bash exited early (rc={outer.returncode})"
            time.sleep(0.2)
        # Keep a handle only on a process that started after this test did:
        # that one is ours, never an unrelated process with a reused PID.
        for pid in started.read_text().split():
            try:
                p = psutil.Process(int(pid))
                if p.create_time() >= t_start - 1:
                    spawned.append(p)
            except psutil.NoSuchProcess:
                pass
        interp = spawned[1]
        assert interp.pid == int(started.read_text().split()[0])

        outer.kill()  # TerminateProcess, as `taskkill /F` does
        outer.wait(10)
        t0 = time.monotonic()
        rc = interp.wait(timeout=15)
        elapsed = time.monotonic() - t0
        assert rc == 143, f"interpreter exit code {rc}"
        assert elapsed < 15
        assert not out.exists() or "survived" not in out.read_text()
    finally:
        for p in reversed(spawned):
            try:
                if p.is_running():
                    p.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(spawned, timeout=5)
        if outer.poll() is None:
            outer.kill()
