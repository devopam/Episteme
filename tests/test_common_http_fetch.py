"""scripts/data/_lib/common.sh `http_fetch`: the curl fallback re-runs (resuming via
`-C -`) only on transient network failures, and fails at once on permanent ones.

Plain `curl --retry` does not retry a connection reset (exit 56); on 2026-10-08 one
reset ended a 3.7 h reactome download. `--retry-all-errors` was rejected: it also
retries permanent errors (a 404 waited ~7.5 min) and curl < 7.71 lacks it.

A fake `curl` on PATH replays a scripted sequence of exit codes and logs its calls.
When aria2c fails, http_fetch retries the unfinished files with the same curl path
(2026-10-11: Windows aria2c failed every TLS handshake with ftp.expasy.org).
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
# replays exit codes from $FAKE_CURL_CODES, one per call; logs each call's args.
# With `-C -` it appends, as a real resume does, so a stale partial shows up.
echo "$*" >> "$FAKE_CURL_LOG"
n=$(wc -l < "$FAKE_CURL_LOG")
code=$(echo $FAKE_CURL_CODES | cut -d' ' -f"$n")
code=${code:-0}
resume=0; case " $* " in *" -C - "*) resume=1 ;; esac
if [ "$code" = 0 ]; then
    while [ "$#" -gt 0 ]; do
        if [ "$1" = -o ]; then
            if [ "$resume" = 1 ]; then echo data >> "$2"; else echo data > "$2"; fi
        fi
        shift
    done
fi
exit "$code"
"""

FAKE_ARIA2C = r"""#!/usr/bin/env bash
# exits $FAKE_ARIA2C_CODE; writes $FAKE_ARIA2C_SESSION (space-separated URLs) as
# the --save-session file, in aria2c's format (URI line, indented options)
echo "$*" >> "$FAKE_ARIA2C_LOG"
for a in "$@"; do
    case "$a" in --save-session=*)
        for u in $FAKE_ARIA2C_SESSION; do printf '%s\t\n gid=1\n' "$u"; done > "${a#*=}" ;;
    esac
done
exit "$FAKE_ARIA2C_CODE"
"""

TOOLS = (
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
)


def _bash() -> str:
    for c in (r"C:\Program Files\Git\bin\bash.exe", "/usr/bin/bash", "/bin/bash"):
        if Path(c).exists():
            return c
    pytest.skip("no POSIX bash")


def _run(
    tmp_path: Path,
    codes: str,
    tries: str = "4",
    aria2c_code: int | None = None,
    urls: tuple[str, ...] = ("https://example.invalid/d/file.txt",),
    session: tuple[str, ...] = (),
) -> tuple[subprocess.CompletedProcess, list[str]]:
    lib = tmp_path / "scripts" / "data" / "_lib"
    shutil.copytree(LIB, lib)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "curl").write_bytes(FAKE_CURL.encode("utf-8"))
    (fake / "curl").chmod(0o755)
    if aria2c_code is not None:
        (fake / "aria2c").write_bytes(FAKE_ARIA2C.encode("utf-8"))
        (fake / "aria2c").chmod(0o755)
    # The curl fallback runs only when aria2c is absent. On POSIX, PATH is just
    # fakebin, holding the fake curl plus links to the tools the scripts need, so
    # an aria2c in /usr/bin can't hide the fallback. Git Bash on Windows keeps
    # /usr/bin (its programs need the msys DLLs there), and aria2c is never there.
    if os.name == "nt":
        path_tail = ":/usr/bin:/bin"
    else:
        path_tail = ""
        for tool in TOOLS:
            found = shutil.which(tool)
            if found:
                (fake / tool).symlink_to(found)
    log = tmp_path / "curl.log"
    dest = tmp_path / "out"

    # PATH entries are ':'-separated, so a Windows `C:/...` path goes through cygpath
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
            "FAKE_CURL_LOG": log.as_posix(),
            "FAKE_CURL_CODES": codes,
            "EPISTEME_CURL_RESUME_TRIES": tries,
            "EPISTEME_CURL_RESUME_DELAY": "0",
            "FAKE_ARIA2C_LOG": (tmp_path / "aria2c.log").as_posix(),
            "FAKE_ARIA2C_CODE": str(aria2c_code or 0),
            "FAKE_ARIA2C_SESSION": " ".join(session),
            "URLS": " ".join(urls),
        }
    )
    proc = subprocess.run(
        [_bash(), "-c", script], env=env, capture_output=True, text=True, timeout=60
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


def test_connection_reset_is_resumed(tmp_path):
    proc, calls = _run(tmp_path, "6 56 18 0")
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 4
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


def test_non_numeric_try_limit_falls_back_instead_of_looping(tmp_path):
    # 20 transient failures; a broken `-ge` test would keep calling curl past them
    proc, calls = _run(tmp_path, " ".join(["56"] * 20), tries="lots")
    assert proc.returncode != 0
    assert len(calls) == 15  # the default limit
    assert "EPISTEME_CURL_RESUME_TRIES not a number" in proc.stderr


def test_aria2c_failure_falls_back_to_curl(tmp_path):
    proc, calls = _run(tmp_path, "0", aria2c_code=1)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "aria2c.log").exists()
    assert len(calls) == 1 and "-C -" in calls[0]
    assert "aria2c exit 1" in proc.stderr
    assert (tmp_path / "out" / "file.txt").read_text().strip() == "data"


def test_aria2c_partial_file_is_restarted_not_appended(tmp_path):
    # segmented aria2c output can have holes; `-C -` would append at its size
    out = tmp_path / "out"
    out.mkdir()
    (out / "file.txt").write_text("partial-with-holes")
    (out / "file.txt.aria2").write_text("control")
    proc, calls = _run(tmp_path, "0", aria2c_code=1)
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 1  # no size probe: the partial was discarded first
    assert not (out / "file.txt.aria2").exists()
    # the fake curl appends on `-C -`: a kept partial would end up in front
    assert (out / "file.txt").read_text() == "data\n"


def test_aria2c_success_does_not_call_curl(tmp_path):
    proc, calls = _run(tmp_path, "0", aria2c_code=0)
    assert proc.returncode == 0, proc.stderr
    assert calls == []


def test_size_match_skip_refuses_file_with_aria2_control_file(tmp_path):
    # 2026-10-13: segmented aria2c writes the last segment early, so a stopped
    # download can be full size with holes; its `.aria2` file is the only sign.
    # Size-matched skip kept two such files and both failed their MD5 check.
    lib = tmp_path / "scripts" / "data" / "_lib"
    shutil.copytree(LIB, lib)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    # a HEAD that always reports the local file's size: only the control file differs
    (fake / "curl").write_bytes(b"#!/usr/bin/env bash\nprintf 'Content-Length: 4\\r\\n'\n")
    (fake / "curl").chmod(0o755)
    data = tmp_path / "f.gz"
    data.write_bytes(b"abcd")
    script = (
        'fb=$(cygpath -u "$FAKEBIN" 2>/dev/null || echo "$FAKEBIN"); '
        'export PATH="$fb:$PATH"; . "$LIB/common.sh"; '
        'size_match_skip "$F" https://example.invalid/f.gz && echo SKIP || echo FETCH'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("EPISTEME_")}
    env.update({"LIB": lib.as_posix(), "FAKEBIN": fake.as_posix(), "F": data.as_posix()})

    def run() -> str:
        proc = subprocess.run(
            [_bash(), "-c", script], env=env, capture_output=True, text=True, timeout=60
        )
        return proc.stdout.strip().splitlines()[-1]

    assert run() == "SKIP"
    (tmp_path / "f.gz.aria2").write_bytes(b"control")
    assert run() == "FETCH"


def test_aria2c_session_limits_curl_to_unfinished_files(tmp_path):
    # aria2c finished a.txt (its server may give no Content-Length and reject a
    # range at EOF, so it must not be re-requested) and failed b.txt
    out = tmp_path / "out"
    out.mkdir()
    (out / "a.txt").write_text("complete-a")
    a, b = "https://example.invalid/d/a.txt", "https://example.invalid/d/b.txt"
    proc, calls = _run(tmp_path, "0", aria2c_code=3, urls=(a, b), session=(b,))
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 1 and b in calls[0]
    assert "retrying 1 unfinished file(s)" in proc.stderr
    assert (out / "a.txt").read_text() == "complete-a"
    assert (out / "b.txt").read_text() == "data\n"
