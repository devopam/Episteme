"""scripts/data/_lib/common.sh `list_manifest` / `_scrape_listing`: a directory listing
gets several attempts before the source fails.

2026-10-18: one failed listing request during a network switch failed the whole
europepmc_abstracts download ("list_manifest: cannot list ..."). A fake `curl` on PATH
replays a scripted sequence of exit codes and prints an autoindex page on success.
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
# exit codes from $FAKE_CURL_CODES, one per call; on 0 prints a small autoindex page
echo "$*" >> "$FAKE_CURL_LOG"
n=$(wc -l < "$FAKE_CURL_LOG")
code=$(echo $FAKE_CURL_CODES | cut -d' ' -f"$n")
code=${code:-0}
if [ "$code" = 0 ]; then
    printf '<a href="?C=N;O=D">Name</a> <a href="/pub/">Parent</a>\n'
    printf '<a href="PreprintAbstractsXML_202608.zip">x</a>\n'
fi
exit "$code"
"""


def _bash() -> str:
    for c in (r"C:\Program Files\Git\bin\bash.exe", "/usr/bin/bash", "/bin/bash"):
        if Path(c).exists():
            return c
    pytest.skip("no POSIX bash")


def _run(tmp_path: Path, codes: str, tries: str = "3"):
    lib = tmp_path / "scripts" / "data" / "_lib"
    shutil.copytree(LIB, lib)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "curl").write_bytes(FAKE_CURL.encode("utf-8"))
    (fake / "curl").chmod(0o755)
    log = tmp_path / "curl.log"
    script = (
        'fb=$(cygpath -u "$FAKEBIN" 2>/dev/null || echo "$FAKEBIN"); '
        'export PATH="$fb:/usr/bin:/bin"; '
        '. "$LIB/common.sh"; list_manifest https://example.invalid/dir'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("EPISTEME_")}
    env.update(
        {
            "LIB": lib.as_posix(),
            "FAKEBIN": fake.as_posix(),
            "FAKE_CURL_LOG": log.as_posix(),
            "FAKE_CURL_CODES": codes,
            "EPISTEME_LIST_TRIES": tries,
            "EPISTEME_LIST_RETRY_DELAY": "0",
        }
    )
    proc = subprocess.run(
        [_bash(), "-c", script], env=env, capture_output=True, text=True, timeout=60
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


def test_listing_succeeds_on_the_first_attempt(tmp_path):
    proc, calls = _run(tmp_path, "0")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["PreprintAbstractsXML_202608.zip"]
    assert len(calls) == 1


def test_a_failed_listing_is_retried(tmp_path):
    proc, calls = _run(tmp_path, "56 28 0")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["PreprintAbstractsXML_202608.zip"]
    assert len(calls) == 3


def test_every_attempt_failing_fails_the_listing(tmp_path):
    proc, calls = _run(tmp_path, "56 56 56 0")
    assert proc.returncode == 1
    assert len(calls) == 3
    assert "cannot list" in proc.stderr
