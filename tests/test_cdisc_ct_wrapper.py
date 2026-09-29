"""Network-free tests for scripts/data/cdisc_ct/download_cdisc_ct.sh (fake curl on PATH)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "scripts" / "data" / "cdisc_ct" / "download_cdisc_ct.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("aria2c") is not None, reason="http_fetch prefers aria2c, bypassing the curl shim"
)

HEADER = "\t".join(
    [
        "Code",
        "Codelist Code",
        "Codelist Extensible (Yes/No)",
        "Codelist Name",
        "CDISC Submission Value",
        "CDISC Synonym(s)",
        "CDISC Definition",
        "NCI Preferred Term",
    ]
)
ROW = "\t".join(["C66731", "", "No", "Sex", "SEX", "Sex", "The sex of the subject.", "Sex"])


def _bash() -> str:
    for c in (r"C:\Program Files\Git\bin\bash.exe", "/usr/bin/bash", "/bin/bash"):
        if Path(c).exists():
            return c
    pytest.skip("no POSIX bash")


def _msys_path(p: Path) -> str:
    q = p.as_posix()  # C:/x -> /c/x so bash accepts it inside PATH on Windows
    return f"/{q[0].lower()}{q[2:]}" if len(q) > 1 and q[1] == ":" else q


def _fake_curl(bindir: Path, log: Path) -> None:
    """HEAD (-I in a short-flag cluster) answers NCI-like headers; GET writes the body.

    FAKE_SEND_MISSING=1 -> any SEND URL answers text/html (NCI's JS-app fallback page).
    FAKE_BAD_BODY=1     -> downloads return text without the 8-column header line.
    FAKE_GET_FAIL=1     -> downloads fail (curl rc 22, nothing written).
    FAKE_HEAD_FAIL=1    -> HEAD requests fail (curl rc 6, as for a DNS error).
    FAKE_LASTMOD=<v>    -> Last-Modified override; "none" omits the header.
    Each call is logged as `HEAD <url>` or `GET <url>`.
    """
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "good.txt").write_bytes(f"{HEADER}\n{ROW}\n".encode())
    (bindir / "bad.txt").write_bytes(b"<html>not a terminology file</html>\n")
    script = bindir / "curl"
    body = (
        "#!/usr/bin/env bash\n"
        f'BIN="{bindir.as_posix()}"\n'
        "out=''; url=''; head=0; prev=''\n"
        'for a in "$@"; do\n'
        '  [ "$prev" = "-o" ] || [ "$prev" = "--output" ] && out="$a"\n'
        '  case "$a" in --*) ;; -*I*) head=1 ;; http*) url="$a" ;; esac\n'
        '  prev="$a"\n'
        "done\n"
        'if [ "${FAKE_BAD_BODY:-0}" = 1 ]; then src="$BIN/bad.txt"; else src="$BIN/good.txt"; fi\n'
        'if [ "$head" = 1 ]; then\n'
        f'  echo "HEAD $url" >> "{log.as_posix()}"\n'
        '  [ "${FAKE_HEAD_FAIL:-0}" = 1 ] && { echo "curl: (6) no host" >&2; exit 6; }\n'
        '  printf "HTTP/1.1 200 OK\\r\\n"\n'
        '  case "$url" in\n'
        '    *SEND*) if [ "${FAKE_SEND_MISSING:-0}" = 1 ]; then\n'
        '              printf "Content-Type: text/html\\r\\n"\n'
        '              printf "Content-Length: 2873\\r\\n\\r\\n"; exit 0\n'
        "            fi ;;\n"
        "  esac\n"
        '  case "$url" in\n'
        '    */Protocol/*) lm="Sat, 11 Jul 2026 23:27:50 GMT" ;;\n'
        '    *) lm="Fri, 25 Sep 2026 15:43:39 GMT" ;;\n'
        "  esac\n"
        '  printf "Content-Type: text/plain\\r\\n"\n'
        '  printf "Content-Length: %s\\r\\n" "$(wc -c < "$src" | tr -d " ")"\n'
        '  [ -n "${FAKE_LASTMOD:-}" ] && lm="$FAKE_LASTMOD"\n'
        '  [ "$lm" = none ] || printf "Last-Modified: %s\\r\\n" "$lm"\n'
        '  printf "\\r\\n"\n'
        "  exit 0\n"
        "fi\n"
        f'echo "GET $url" >> "{log.as_posix()}"\n'
        '[ "${FAKE_GET_FAIL:-0}" = 1 ] && { echo "curl: (22) error" >&2; exit 22; }\n'
        'if [ -n "$out" ]; then mkdir -p "$(dirname "$out")"; cat "$src" > "$out"\n'
        'else cat "$src"; fi\n'
    )
    script.write_bytes(body.encode())  # LF endings: a CRLF shebang breaks bash on Windows
    script.chmod(0o755)


def _fake_date(bindir: Path) -> None:
    """BSD-style `date` shim: rejects GNU `-d`, honors `-j -f INFMT VALUE OUTFMT`.

    Delegates the actual conversion to the real (GNU) date found via PATH before
    this shim's bindir was prepended, so the test runs unmodified on Windows Git
    Bash and on Linux.
    """
    real_date = shutil.which("date")
    if not real_date:
        pytest.skip("no real `date` on PATH to back the fake BSD date shim")
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / "date"
    body = (
        "#!/usr/bin/env bash\n"
        f'REAL_DATE="{_msys_path(Path(real_date))}"\n'
        'have_d=0; have_j=0; value=""; outfmt=""\n'
        'args=("$@"); i=0\n'
        'while [ "$i" -lt "${#args[@]}" ]; do\n'
        '  a="${args[$i]}"\n'
        '  case "$a" in\n'
        "    -d) have_d=1 ;;\n"
        "    -j) have_j=1 ;;\n"
        '    -f) i=$((i + 1)); i=$((i + 1)); value="${args[$i]}" ;;\n'
        '    +*) outfmt="$a" ;;\n'
        "  esac\n"
        "  i=$((i + 1))\n"
        "done\n"
        'if [ "$have_d" = 1 ] && [ "$have_j" != 1 ]; then\n'
        '  echo "date: illegal option -- d (fake BSD date)" >&2\n'
        "  exit 1\n"
        "fi\n"
        'if [ "$have_j" = 1 ]; then\n'
        '  exec "$REAL_DATE" -u -d "$value" "$outfmt"\n'
        "fi\n"
        'exec "$REAL_DATE" "$@"\n'
    )
    script.write_bytes(body.encode())  # LF endings: a CRLF shebang breaks bash on Windows
    script.chmod(0o755)


def _run(tmp_path, *args, extra_env=None, fake_date=False):
    log = tmp_path / "curl.log"
    if log.exists():
        log.unlink()
    _fake_curl(tmp_path / "bin", log)
    if fake_date:
        _fake_date(tmp_path / "bin")
    env = {
        **os.environ,
        "SHIM_DIR": _msys_path(tmp_path / "bin"),
        "EPISTEME_ACTOR": "episteme_sys_admin",
        "EPISTEME_DATA_ROOT": str(tmp_path),
        "EPISTEME_RAW_ROOT": str(tmp_path / "01_raw"),
        "PGDATABASE": "episteme_test",
        **(extra_env or {}),
    }
    proc = subprocess.run(
        # Git-for-Windows bash prepends /mingw64/bin (real curl) to PATH at startup, so put the
        # shim first from *inside* a bash -c, then exec the wrapper.
        [_bash(), "-c", 'PATH="$SHIM_DIR:$PATH"; exec bash "$0" "$@"', str(WRAPPER), *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


def _dest(tmp_path) -> Path:
    return tmp_path / "01_raw" / "cdisc_ct"


def _pkg_dirs(tmp_path) -> set[str]:
    d = _dest(tmp_path)
    return {p.name for p in d.iterdir() if p.is_dir()} if d.exists() else set()


def test_writes_per_package_dated_folders(tmp_path):
    proc, calls = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    d = _dest(tmp_path)
    assert _pkg_dirs(tmp_path) == {"SDTM", "SEND", "ADaM", "Define-XML", "Protocol"}
    for pkg in ("SDTM", "SEND", "ADaM", "Define-XML"):
        rel = d / pkg / "2026-09-25"
        f = rel / f"{pkg}_Terminology.txt"
        assert f.is_file(), pkg
        assert f.read_text(encoding="utf-8").splitlines()[0] == HEADER
        assert (rel / "last_sync_utc.txt").is_file(), pkg
    proto = d / "Protocol" / "2026-07-11"
    assert (proto / "Protocol_Terminology.txt").is_file()
    assert not (d / "Protocol" / "2026-09-25").exists()
    for rel in [d / p / "2026-09-25" for p in ("SDTM", "SEND", "ADaM", "Define-XML")] + [proto]:
        prov = (rel / "PROVENANCE.txt").read_text(encoding="utf-8")
        pkg = rel.parent.name
        assert f"/{pkg}/{pkg}%20Terminology.txt" in prov, pkg
        assert "free to use without licensing restrictions" in prov
        assert "retrieved_at: 20" in prov
    prov = (proto / "PROVENANCE.txt").read_text(encoding="utf-8")
    assert "Sat, 11 Jul 2026 23:27:50 GMT" in prov
    gets = [c for c in calls if c.startswith("GET ")]
    assert len(gets) == 5
    assert all(c.endswith("%20Terminology.txt") for c in gets)


def test_html_fallback_package_is_skipped_others_download(tmp_path):
    proc, calls = _run(tmp_path, extra_env={"FAKE_SEND_MISSING": "1"})
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "SEND not available" in proc.stderr
    assert "text/html" in proc.stderr
    assert not (_dest(tmp_path) / "SEND").exists()
    assert (_dest(tmp_path) / "SDTM" / "2026-09-25" / "SDTM_Terminology.txt").is_file()
    assert (_dest(tmp_path) / "Protocol" / "2026-07-11" / "Protocol_Terminology.txt").is_file()
    assert not any(c.startswith("GET ") and "/SEND/" in c for c in calls)


def test_dry_run_writes_nothing(tmp_path):
    proc, calls = _run(tmp_path, "--dry-run")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert not _dest(tmp_path).exists()
    assert proc.stderr.count("DRY: would fetch") == 5
    assert "SDTM/2026-09-25/SDTM_Terminology.txt" in proc.stderr
    assert not any(c.startswith("GET ") for c in calls)


def test_max_files_limits_packages_in_order(tmp_path):
    proc, calls = _run(tmp_path, "--max-files", "2")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _pkg_dirs(tmp_path) == {"SDTM", "SEND"}
    assert (_dest(tmp_path) / "SEND" / "2026-09-25" / "SEND_Terminology.txt").is_file()
    assert not any("/ADaM/" in c or "/Protocol/" in c for c in calls)


def test_rerun_same_release_skips_download(tmp_path):
    proc, _ = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    prov = _dest(tmp_path) / "SDTM" / "2026-09-25" / "PROVENANCE.txt"
    prov.write_text("sentinel\n", encoding="utf-8")
    proc, calls = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert calls, "shim was not called"
    assert all(c.startswith("HEAD ") for c in calls), calls
    assert proc.stderr.count("up to date") == 5
    assert prov.read_text(encoding="utf-8") == "sentinel\n"


def test_force_refetches_same_release(tmp_path):
    proc, _ = _run(tmp_path, "--max-files", "1")
    assert proc.returncode == 0, proc.stderr[-2000:]
    proc, calls = _run(tmp_path, "--max-files", "1", "--force")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert [c for c in calls if c.startswith("GET ")] and "up to date" not in proc.stderr
    f = _dest(tmp_path) / "SDTM" / "2026-09-25" / "SDTM_Terminology.txt"
    assert f.read_text(encoding="utf-8") == f"{HEADER}\n{ROW}\n"  # replaced, not appended


def test_bad_header_after_download_is_rejected(tmp_path):
    proc, calls = _run(tmp_path, "--max-files", "1", extra_env={"FAKE_BAD_BODY": "1"})
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert any(c.startswith("GET ") for c in calls)
    assert "unexpected header" in proc.stderr
    rel = _dest(tmp_path) / "SDTM" / "2026-09-25"
    assert not (rel / "SDTM_Terminology.txt").exists()
    assert not (rel / "PROVENANCE.txt").exists()
    assert not (rel / "last_sync_utc.txt").exists()


def test_force_with_failed_fetch_keeps_existing_file_and_provenance(tmp_path):
    proc, _ = _run(tmp_path, "--max-files", "1")
    assert proc.returncode == 0, proc.stderr[-2000:]
    rel = _dest(tmp_path) / "SDTM" / "2026-09-25"
    before = {n: (rel / n).read_bytes() for n in ("SDTM_Terminology.txt", "PROVENANCE.txt")}
    proc, calls = _run(tmp_path, "--max-files", "1", "--force", extra_env={"FAKE_GET_FAIL": "1"})
    assert proc.returncode == 1, proc.stderr[-2000:]
    assert any(c.startswith("GET ") for c in calls)
    assert "SDTM fetch failed" in proc.stderr
    for name, data in before.items():
        assert (rel / name).read_bytes() == data, name
    assert not (rel / "SDTM_Terminology.txt.part").exists()


def test_first_fetch_failure_leaves_no_folder(tmp_path):
    proc, _ = _run(tmp_path, "--max-files", "1", extra_env={"FAKE_GET_FAIL": "1"})
    assert proc.returncode == 1, proc.stderr[-2000:]
    assert not (_dest(tmp_path) / "SDTM").exists()


@pytest.mark.parametrize("lastmod", ["none", "not a date"])
def test_missing_or_unparseable_last_modified_skips_package(tmp_path, lastmod):
    proc, calls = _run(tmp_path, "--max-files", "1", extra_env={"FAKE_LASTMOD": lastmod})
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "SDTM has no parseable Last-Modified" in proc.stderr
    assert not _dest(tmp_path).exists()  # never a folder dated today
    assert not any(c.startswith("GET ") for c in calls)


def test_lastmod_parses_via_bsd_date_fallback(tmp_path):
    # A fake `date` rejects GNU `-d` first, forcing the wrapper's BSD `-j -f`
    # fallback; the shim delegates that to the real date so the release date
    # still comes out right. --dry-run's logged path proves the parse worked
    # without the empty/garbage guard turning it into today's date.
    proc, calls = _run(tmp_path, "--dry-run", "--max-files", "1", fake_date=True)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "SDTM/2026-09-25/SDTM_Terminology.txt" in proc.stderr
    assert not any(c.startswith("GET ") for c in calls)


@pytest.mark.parametrize("lastmod", ["none", "not a date"])
def test_missing_or_unparseable_last_modified_skips_package_bsd_date(tmp_path, lastmod):
    # Same guard as test_missing_or_unparseable_last_modified_skips_package, but
    # with the BSD-only `date` shim: both the GNU attempt and the BSD fallback
    # must fail cleanly on empty/garbage input, never producing today's date.
    proc, calls = _run(
        tmp_path, "--max-files", "1", extra_env={"FAKE_LASTMOD": lastmod}, fake_date=True
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "SDTM has no parseable Last-Modified" in proc.stderr
    assert not _dest(tmp_path).exists()
    assert not any(c.startswith("GET ") for c in calls)


def test_unreachable_head_warns_and_skips(tmp_path):
    proc, calls = _run(tmp_path, "--max-files", "1", extra_env={"FAKE_HEAD_FAIL": "1"})
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "could not reach NCI for SDTM" in proc.stderr
    assert "Content-Type 'none'" not in proc.stderr
    assert not _dest(tmp_path).exists()
    assert not any(c.startswith("GET ") for c in calls)
