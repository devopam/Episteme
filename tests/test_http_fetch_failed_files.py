"""http_fetch: pause points between batches, and failed_files.txt.

A failed file is recorded and the download goes on; http_fetch returns 1.
Fake curl and fake PYTHON on PATH; no network, nothing outside tmp_path."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "scripts" / "data" / "_lib"

FAKE_CURL = r"""#!/usr/bin/env bash
# fails (exit 22) for any URL containing "bad"; else writes the output file
url="${@: -1}"
case "$url" in *bad*) exit 22 ;; esac
while [ "$#" -gt 0 ]; do
    if [ "$1" = -o ]; then echo data > "$2"; fi
    shift
done
exit 0
"""
FAKE_PY = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_PY_LOG"
exit 0
"""


def _bash() -> str:
    for c in (r"C:\Program Files\Git\bin\bash.exe", "/usr/bin/bash", "/bin/bash"):
        if Path(c).exists():
            return c
    pytest.skip("no POSIX bash")


def _run(tmp_path, urls, *, batch=None, run_id=None, dry=False, run_dir=None):
    lib = tmp_path / "scripts" / "data" / "_lib"
    shutil.copytree(LIB, lib)
    fake = tmp_path / "fakebin"
    fake.mkdir()
    for name, text in (("curl", FAKE_CURL), ("fakepy", FAKE_PY)):
        (fake / name).write_bytes(text.encode("utf-8"))
        (fake / name).chmod(0o755)
    if os.name != "nt":
        for tool in (
            "bash",
            "env",
            "date",
            "mkdir",
            "dirname",
            "basename",
            "mktemp",
            "rm",
            "sleep",
            "wc",
            "cut",
            "cat",
        ):
            found = shutil.which(tool)
            if found:
                (fake / tool).symlink_to(found)
    path_tail = ":/usr/bin:/bin" if os.name == "nt" else ""
    dest = tmp_path / "out"
    pylog = tmp_path / "py.log"
    script = (
        'fb=$(cygpath -u "$FAKEBIN" 2>/dev/null || echo "$FAKEBIN"); '
        'export PATH="$fb$PATH_TAIL"; '
        '. "$LIB/common.sh"; http_fetch "$DEST" $URLS'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("EPISTEME_")}
    env.update(
        {
            "LIB": lib.as_posix(),
            "FAKEBIN": fake.as_posix(),
            "PATH_TAIL": path_tail,
            "DEST": dest.as_posix(),
            "URLS": " ".join(urls),
            "PYTHON": "fakepy",
            "FAKE_PY_LOG": pylog.as_posix(),
            "EPISTEME_CURL_RESUME_DELAY": "0",
            "EPISTEME_DATA_ROOT": (tmp_path / "data").as_posix(),
            "EPISTEME_PROCESSED_ROOT": (tmp_path / "proc").as_posix(),
            "EPISTEME_RAW_ROOT": (tmp_path / "raw").as_posix(),
            "EPISTEME_LOCK_DIR": (tmp_path / "locks").as_posix(),
        }
    )
    if batch:
        env["EPISTEME_FETCH_BATCH"] = batch
    if run_id:
        env["EPISTEME_RUN_ID"] = run_id
    if run_dir:
        env["EPISTEME_RUN_DIR"] = run_dir.as_posix()
    if dry:
        env["EPISTEME_DRY_RUN"] = "1"
    proc = subprocess.run(
        [_bash(), "-c", script], env=env, capture_output=True, text=True, timeout=60
    )
    calls = pylog.read_text(encoding="utf-8").splitlines() if pylog.exists() else []
    return proc, calls, dest


URLS3 = (
    "https://example.invalid/a.txt",
    "https://example.invalid/bad.txt",
    "https://example.invalid/c.txt",
)


def test_one_failed_file_is_recorded_and_the_rest_fetched(tmp_path):
    run_dir = tmp_path / "run"
    proc, _, dest = _run(tmp_path, URLS3, run_dir=run_dir)
    assert proc.returncode == 1, proc.stderr
    assert (dest / "a.txt").exists() and (dest / "c.txt").exists()
    assert not (dest / "bad.txt").exists()
    lines = (run_dir / "failed_files.txt").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    url, rel, reason = lines[0].split("\t")
    assert url.endswith("/bad.txt") and rel == "bad.txt" and reason
    assert "1 file(s) failed; see" in proc.stderr


def test_failed_list_defaults_to_the_dest_dir(tmp_path):
    proc, _, dest = _run(tmp_path, URLS3)
    assert proc.returncode == 1
    assert len((dest / ".failed_files.txt").read_text(encoding="utf-8").splitlines()) == 1


def test_all_good_returns_zero(tmp_path):
    proc, _, dest = _run(tmp_path, URLS3[::2])
    assert proc.returncode == 0, proc.stderr
    assert not (dest / ".failed_files.txt").exists()


def test_pause_point_runs_once_per_batch(tmp_path):
    proc, calls, _ = _run(
        tmp_path, URLS3[::2] + ("https://example.invalid/d.txt",), batch="1", run_id="R1"
    )
    assert proc.returncode == 0, proc.stderr
    pauses = [c for c in calls if "pause-point" in c]
    assert len(pauses) == 3
    assert all("--run-id R1" in c for c in pauses)
    assert sum("progress" in c and "batch 3/3" in c for c in calls) == 1
    assert sum("long-begin" in c for c in calls) == 3
    assert sum("long-end" in c for c in calls) == 3


def test_no_run_id_means_no_pause_point(tmp_path):
    _, calls, _ = _run(tmp_path, URLS3[::2], batch="1")
    assert calls == []


def test_dry_run_calls_no_pause_point(tmp_path):
    proc, calls, _ = _run(tmp_path, URLS3, batch="1", run_id="R1", dry=True)
    assert proc.returncode == 0
    assert calls == []


def test_bad_batch_size_warns_and_uses_64(tmp_path):
    proc, calls, _ = _run(tmp_path, URLS3[::2], batch="many", run_id="R1")
    assert "EPISTEME_FETCH_BATCH not a positive number" in proc.stderr
    assert sum("pause-point" in c for c in calls) == 1


def test_batch_size_with_leading_zero_is_decimal(tmp_path):
    proc, calls, _ = _run(tmp_path, URLS3[::2], batch="01", run_id="R1")
    assert "not a positive number" not in proc.stderr
    assert sum("pause-point" in c for c in calls) == 2  # 1 URL per batch


def test_batch_size_08_is_not_an_octal_error(tmp_path):
    proc, calls, _ = _run(tmp_path, URLS3[::2], batch="08", run_id="R1")
    assert "not a positive number" not in proc.stderr and "value too great" not in proc.stderr
    assert sum("pause-point" in c for c in calls) == 1  # both URLs in one batch of 8


def test_zero_batch_size_warns_and_uses_64(tmp_path):
    proc, calls, _ = _run(tmp_path, URLS3[::2], batch="00", run_id="R1")
    assert "EPISTEME_FETCH_BATCH not a positive number" in proc.stderr
    assert sum("pause-point" in c for c in calls) == 1
