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
import sys
import zipfile
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
            if [ -n "$FAKE_CURL_BODY" ]; then cp "$FAKE_CURL_BODY" "$2"
            elif [ "$resume" = 1 ]; then echo data >> "$2"; else echo data > "$2"; fi
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
    "tail",
    "grep",
    "cp",
    "mv",
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
    extra_env: dict[str, str] | None = None,
    precreate: dict[str, bytes] | None = None,
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
            "FAKE_CURL_BODY": "",
            "PYTHON": Path(sys.executable).as_posix(),
        }
    )
    env.update(extra_env or {})
    for rel, data in (precreate or {}).items():
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        (dest / rel).write_bytes(data)
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


def _skip_script_env(
    tmp_path: Path,
    name: str,
    content: bytes,
    dry_run: bool = False,
    marker: str | None = None,
    python: str | None = None,
    pre: str = "",
):
    lib = tmp_path / "scripts" / "data" / "_lib"
    shutil.copytree(LIB, lib)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    n = len(content)
    head = f"#!/usr/bin/env bash\nprintf 'Content-Length: {n}\r\n'\n"
    (fake / "curl").write_bytes(head.encode())
    (fake / "curl").chmod(0o755)
    data = tmp_path / "d" / name
    data.parent.mkdir()
    data.write_bytes(content)
    if marker is not None:
        st = data.stat()
        mtime = st.st_mtime_ns + (0 if marker == "match" else 1)
        size = st.st_size
        Path(str(data) + ".verified").write_bytes(f"{size} {mtime} full\n".encode())
    script = (
        'fb=$(cygpath -u "$FAKEBIN" 2>/dev/null || echo "$FAKEBIN"); '
        'export PATH="$fb:$PATH"; . "$LIB/common.sh"; ' + pre + " "
        'size_match_skip "$F" https://example.invalid/f && echo SKIP || echo FETCH'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("EPISTEME_")}
    env.update(
        {
            "LIB": lib.as_posix(),
            "FAKEBIN": fake.as_posix(),
            "F": data.as_posix(),
            "PYTHON": python or Path(sys.executable).as_posix(),
        }
    )
    if dry_run:
        env["EPISTEME_DRY_RUN"] = "1"
    proc = subprocess.run(
        [_bash(), "-c", script], env=env, capture_output=True, text=True, timeout=60
    )
    return proc.stdout.strip().splitlines()[-1], data


def _good_zip(tmp_path: Path) -> bytes:
    z = tmp_path / "good.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("a.xml", "<a/>" * 50)
    return z.read_bytes()


def _damaged(tmp_path: Path) -> bytes:
    """Full size, tail zeroed: the central directory is gone, as after a stopped aria2c."""
    good = _good_zip(tmp_path)
    return good[:-30] + bytes(30)


def test_size_match_skip_keeps_an_intact_zip(tmp_path):
    out, data = _skip_script_env(tmp_path, "x.zip", _good_zip(tmp_path))
    assert out == "SKIP" and data.is_file()


def test_size_match_skip_moves_a_damaged_full_size_zip_aside(tmp_path):
    # 2026-11-05: full-size DailyMed zips left by a stopped download passed the size check.
    damaged = _damaged(tmp_path)
    out, data = _skip_script_env(tmp_path, "x.zip", damaged)
    assert out == "FETCH"
    assert not data.exists()
    bad = list(data.parent.glob("_bad_*/x.zip.bad"))
    assert len(bad) == 1 and bad[0].read_bytes() == damaged


def test_dry_run_never_tests_moves_or_marks_a_zip(tmp_path):
    # a dry run does not read the file: it reports "would verify" and leaves everything alone
    damaged = _damaged(tmp_path)
    out, data = _skip_script_env(tmp_path, "x.zip", damaged, dry_run=True)
    assert out == "SKIP" and data.read_bytes() == damaged
    assert not Path(str(data) + ".verified").exists()
    assert not list(data.parent.glob("_bad_*"))


def test_a_non_zip_file_is_not_tested(tmp_path):
    out, data = _skip_script_env(tmp_path, "x.gz", b"not a zip at all")
    assert out == "SKIP" and data.is_file()


def test_a_zip_that_downloads_damaged_is_recorded_failed_and_moved(tmp_path):
    # the fake curl writes "data" (not a zip): the fetch "succeeds" but the file is damaged
    proc, _ = _run(tmp_path, "0", urls=("https://example.invalid/d/x.zip",))
    assert proc.returncode != 0
    dest = tmp_path / "out"
    assert not (dest / "x.zip").exists()
    assert len(list(dest.glob("_bad_*/x.zip.bad"))) == 1


def _zero_middle_zip(tmp_path: Path) -> bytes:
    """A stored zip whose middle is zero-filled: tail and central directory intact."""
    z = tmp_path / "mid.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("a.xml", bytes(range(256)) * 40)
    raw = bytearray(z.read_bytes())
    raw[3000:6000] = bytes(3000)
    return bytes(raw)


def test_size_skip_runs_the_full_test_so_a_zero_filled_middle_is_quarantined(tmp_path):
    # 2026-11-10: six DailyMed zips with an intact tail and an unfetched middle passed the
    # directory-only check. No marker means the full CRC test runs.
    damaged = _zero_middle_zip(tmp_path)
    out, data = _skip_script_env(tmp_path, "x.zip", damaged)
    assert out == "FETCH" and not data.exists()
    bad = list(data.parent.glob("_bad_*/x.zip.bad"))
    assert len(bad) == 1 and bad[0].read_bytes() == damaged
    assert not Path(str(data) + ".verified").exists()


def test_size_skip_writes_a_marker_after_the_full_test_passes(tmp_path):
    out, data = _skip_script_env(tmp_path, "x.zip", _good_zip(tmp_path))
    marker = Path(str(data) + ".verified")
    st = data.stat()
    assert out == "SKIP" and marker.is_file()
    assert marker.read_text().split() == [str(st.st_size), str(st.st_mtime_ns), "full"]


def test_size_skip_trusts_a_matching_marker_without_the_full_test(tmp_path):
    # the content would fail the full test; only the marker lets it through
    out, data = _skip_script_env(tmp_path, "x.zip", _zero_middle_zip(tmp_path), marker="match")
    assert out == "SKIP" and data.is_file()


def test_size_skip_ignores_a_stale_marker(tmp_path):
    out, data = _skip_script_env(tmp_path, "x.zip", _zero_middle_zip(tmp_path), marker="stale")
    assert out == "FETCH" and not data.exists()
    assert not Path(str(data) + ".verified").exists()


def test_dry_run_writes_no_marker(tmp_path):
    out, data = _skip_script_env(tmp_path, "x.zip", _good_zip(tmp_path), dry_run=True)
    assert out == "SKIP" and not Path(str(data) + ".verified").exists()


def test_a_fresh_download_with_a_zero_filled_middle_fails_the_full_crc_test(tmp_path):
    body = tmp_path / "body.zip"
    body.write_bytes(_zero_middle_zip(tmp_path))
    proc, _ = _run(
        tmp_path,
        "0",
        urls=("https://example.invalid/d/x.zip",),
        extra_env={"FAKE_CURL_BODY": body.as_posix()},
    )
    dest = tmp_path / "out"
    assert proc.returncode != 0
    assert not (dest / "x.zip").exists()
    assert len(list(dest.glob("_bad_*/x.zip.bad"))) == 1


def test_an_intact_fresh_zip_is_kept(tmp_path):
    body = tmp_path / "body.zip"
    body.write_bytes(_good_zip(tmp_path))
    proc, _ = _run(
        tmp_path,
        "0",
        urls=("https://example.invalid/d/x.zip",),
        extra_env={"FAKE_CURL_BODY": body.as_posix()},
    )
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "out" / "x.zip").is_file()
    assert (tmp_path / "out" / "x.zip.verified").is_file()


def test_a_python_that_does_not_run_never_quarantines_a_zip(tmp_path):
    # the Microsoft Store stub: found on PATH, exits non-zero for everything
    stub = tmp_path / "stub_python"
    stub.write_bytes(b"#!/usr/bin/env bash" + chr(10).encode() + b"exit 9" + chr(10).encode())
    stub.chmod(0o755)
    body = tmp_path / "body.zip"
    body.write_bytes(_good_zip(tmp_path))
    proc, _ = _run(
        tmp_path,
        "0",
        urls=("https://example.invalid/d/x.zip",),
        extra_env={"FAKE_CURL_BODY": body.as_posix(), "PYTHON": stub.as_posix()},
    )
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "out" / "x.zip").is_file()
    assert not list((tmp_path / "out").glob("_bad_*"))


def test_a_zip_that_curl_did_not_finish_is_recorded_once_and_not_quarantined(tmp_path):
    # HEAD then fetch both answer 22 (permanent): the partial file stays for a resume
    proc, _ = _run(
        tmp_path,
        "22 22",
        urls=("https://example.invalid/d/x.zip",),
        precreate={"x.zip": b"partial"},
    )
    dest = tmp_path / "out"
    assert proc.returncode != 0
    assert (dest / "x.zip").read_bytes() == b"partial"
    assert not list(dest.glob("_bad_*"))
    failed = (dest / ".failed_files.txt").read_text(encoding="utf-8").splitlines()
    assert len(failed) == 1


def _stub(tmp_path: Path, body: str) -> str:
    stub = tmp_path / "stub_python"
    stub.write_bytes(("#!/usr/bin/env bash\n" + body + "\n").encode())
    stub.chmod(0o755)
    return stub.as_posix()


def _has_unzip() -> bool:
    proc = subprocess.run([_bash(), "-c", "command -v unzip"], capture_output=True, text=True)
    return proc.returncode == 0


def test_without_python_a_damaged_zip_is_quarantined_by_the_unzip_exit_code(tmp_path):
    # unzip -t exits 2 (CRC error) or 9 (bad zip), not 1: any non-zero exit means damaged
    if not _has_unzip():
        pytest.skip("no unzip")
    damaged = _zero_middle_zip(tmp_path)
    out, data = _skip_script_env(tmp_path, "x.zip", damaged, python=_stub(tmp_path, "exit 9"))
    assert out == "FETCH" and not data.exists()
    assert len(list(data.parent.glob("_bad_*/x.zip.bad"))) == 1


def test_without_python_an_intact_zip_is_kept_and_marked(tmp_path):
    if not _has_unzip():
        pytest.skip("no unzip")
    out, data = _skip_script_env(
        tmp_path, "x.zip", _good_zip(tmp_path), python=_stub(tmp_path, "exit 9")
    )
    assert out == "SKIP" and Path(str(data) + ".verified").is_file()


def test_a_file_that_changes_during_the_full_test_is_not_skipped_or_marked(tmp_path):
    # the stub passes the probe, answers the stamp call with the real python, and changes
    # the mtime of the zip while the "full test" runs
    real = Path(sys.executable).as_posix()
    body = (
        'if [ "$1" = "-c" ]; then exit 0; fi; '
        f'if [ "$5" != "full" ]; then exec "{real}" "$@"; fi; '
        'touch -d 2001-01-01 "$4"; exit 0'
    )
    out, data = _skip_script_env(
        tmp_path, "x.zip", _good_zip(tmp_path), python=_stub(tmp_path, body)
    )
    assert out == "FETCH" and data.is_file()
    assert not Path(str(data) + ".verified").exists()


def test_a_zip_that_cannot_be_tested_at_all_is_fetched_not_skipped(tmp_path):
    # no python and no unzip: zip_intact answers 2 -- the zip is not accepted unverified
    out, data = _skip_script_env(
        tmp_path, "x.zip", _good_zip(tmp_path), pre="zip_intact() { return 2; };"
    )
    assert out == "FETCH" and data.is_file()
    assert not Path(str(data) + ".verified").exists()


def test_the_stamp_has_nanosecond_mtime(tmp_path):
    lib = tmp_path / "scripts" / "data" / "_lib"
    shutil.copytree(LIB, lib)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    f = tmp_path / "f.zip"
    f.write_bytes(b"abc")
    env = {k: v for k, v in os.environ.items() if not k.startswith("EPISTEME_")}
    env.update(
        {"LIB": lib.as_posix(), "F": f.as_posix(), "PYTHON": Path(sys.executable).as_posix()}
    )
    proc = subprocess.run(
        [_bash(), "-c", '. "$LIB/common.sh"; _zip_stamp "$F"'],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.stdout.split() == ["3", str(f.stat().st_mtime_ns)]


def test_a_restart_of_an_unfinished_download_removes_the_old_marker(tmp_path):
    proc, _ = _run(
        tmp_path,
        "0",
        urls=("https://example.invalid/d/f.bin",),
        precreate={"f.bin": b"old", "f.bin.aria2": b"x", "f.bin.verified": b"3 1 full\n"},
    )
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "out" / "f.bin.verified").exists()
