"""scripts/data/_lib/common.sh `http_fetch`: the curl fallback re-runs (resuming via
`-C -`) only on transient network failures, and fails at once on permanent ones.

Plain `curl --retry` does not retry a connection reset (exit 56); on 2026-10-08 one
reset ended a 3.7 h reactome download. `--retry-all-errors` was rejected: it also
retries permanent errors (a 404 waited ~7.5 min) and curl < 7.71 lacks it.

A fake `curl` on PATH replays a scripted sequence of exit codes and logs its calls.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "scripts" / "data" / "_lib"

FAKE_CURL = r"""#!/usr/bin/env bash
# replays exit codes from $FAKE_CURL_CODES, one per call; logs each call's args
echo "$*" >> "$FAKE_CURL_LOG"
n=$(wc -l < "$FAKE_CURL_LOG")
code=$(echo $FAKE_CURL_CODES | cut -d' ' -f"$n")
code=${code:-0}
if [ "$code" = 0 ]; then
    while [ "$#" -gt 0 ]; do [ "$1" = -o ] && { echo data > "$2"; }; shift; done
fi
exit "$code"
"""


def _bash() -> str:
    for c in (r"C:\Program Files\Git\bin\bash.exe", "/usr/bin/bash", "/bin/bash"):
        if Path(c).exists():
            return c
    pytest.skip("no POSIX bash")


def _run(tmp_path: Path, codes: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    if any(Path(d, "aria2c").exists() for d in ("/usr/bin", "/bin")):
        pytest.skip("aria2c in a system dir; the curl fallback can't be reached")
    lib = tmp_path / "scripts" / "data" / "_lib"
    shutil.copytree(LIB, lib)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "curl").write_bytes(FAKE_CURL.encode("utf-8"))
    (fake / "curl").chmod(0o755)
    log = tmp_path / "curl.log"
    dest = tmp_path / "out"

    # PATH entries are ':'-separated, so a Windows `C:/...` path goes through cygpath
    script = (
        'fb=$(cygpath -u "$FAKEBIN" 2>/dev/null || echo "$FAKEBIN"); '
        'export PATH="$fb:/usr/bin:/bin"; '
        '. "$LIB/common.sh"; http_fetch "$DEST" "https://example.invalid/d/file.txt"'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("EPISTEME_")}
    env.update(
        {
            "LIB": lib.as_posix(),
            "FAKEBIN": fake.as_posix(),
            "DEST": dest.as_posix(),
            "FAKE_CURL_LOG": log.as_posix(),
            "FAKE_CURL_CODES": codes,
            "EPISTEME_CURL_RESUME_TRIES": "4",
            "EPISTEME_CURL_RESUME_DELAY": "0",
        }
    )
    proc = subprocess.run(
        [_bash(), "-c", script], env=env, capture_output=True, text=True, timeout=60
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


def test_connection_reset_is_resumed(tmp_path):
    proc, calls = _run(tmp_path, "56 18 0")
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 3
    assert all("-C -" in c and "--retry 15" in c for c in calls)
    assert all("--retry-all-errors" not in c for c in calls)
    assert (tmp_path / "out" / "file.txt").exists()


def test_permanent_error_fails_without_rerun(tmp_path):
    proc, calls = _run(tmp_path, "22")
    assert proc.returncode != 0
    assert len(calls) == 1
    assert "curl failed" in proc.stdout + proc.stderr


def test_transient_errors_give_up_after_the_try_limit(tmp_path):
    proc, calls = _run(tmp_path, "56 56 56 56 56 56")
    assert proc.returncode != 0
    assert len(calls) == 4
