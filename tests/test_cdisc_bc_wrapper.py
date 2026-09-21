"""Network-free tests for scripts/data/cdisc_bc/download_cdisc_bc.sh (fake curl on PATH)."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "scripts" / "data" / "cdisc_bc" / "download_cdisc_bc.sh"
SHA = "031429b1d14823721991cd23ee88a11616686ce3"

pytestmark = pytest.mark.skipif(
    shutil.which("aria2c") is not None, reason="http_fetch prefers aria2c, bypassing the curl shim"
)

RAW = "https://raw.githubusercontent.com/cdisc-org/COSMoS/" + SHA
CONTENTS = """[
  {
    "name": "archive",
    "path": "export/archive",
    "size": 0,
    "download_url": null,
    "type": "dir"
  },
  {
    "name": "bc_latest.csv",
    "path": "export/bc_latest.csv",
    "size": 11,
    "download_url": "@RAW@/export/bc_latest.csv",
    "type": "file"
  },
  {
    "name": "sdtm_latest.csv",
    "path": "export/sdtm_latest.csv",
    "size": 12,
    "download_url": "@RAW@/export/sdtm_latest.csv",
    "type": "file"
  }
]
""".replace("@RAW@", RAW)
COMMIT = json.dumps({"sha": SHA, "node_id": "x", "commit": {"sha": "f" * 40}}, indent=2) + "\n"


def _bash() -> str:
    for c in (r"C:\Program Files\Git\bin\bash.exe", "/usr/bin/bash", "/bin/bash"):
        if Path(c).exists():
            return c
    pytest.skip("no POSIX bash")


def _msys_path(p: Path) -> str:
    q = p.as_posix()  # C:/x -> /c/x so bash accepts it inside PATH on Windows
    return f"/{q[0].lower()}{q[2:]}" if len(q) > 1 and q[1] == ":" else q


def _fake_curl(bindir: Path, log: Path) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "contents.json").write_text(CONTENTS, encoding="utf-8")
    (bindir / "commit.json").write_text(COMMIT, encoding="utf-8")
    script = bindir / "curl"
    body = (
        "#!/usr/bin/env bash\n"
        f'BIN="{bindir.as_posix()}"\n'
        "out=''; url=''; head=0; prev=''\n"
        'for a in "$@"; do\n'
        '  [ "$prev" = "-o" ] || [ "$prev" = "--output" ] && out="$a"\n'
        '  case "$a" in -I|-sSIL|-sIL|-fsSI) head=1 ;; http*) url="$a" ;; esac\n'
        '  prev="$a"\n'
        "done\n"
        f'echo "$url" >> "{log.as_posix()}"\n'
        'case "$url" in\n'
        '  */contents/export*) body="$(cat "$BIN/contents.json")" ;;\n'
        '  */commits/*) body="$(cat "$BIN/commit.json")" ;;\n'
        '  */LICENSE) body="licence text" ;;\n'
        '  *) body="data of $(basename "$url")" ;;\n'
        "esac\n"
        'if [ "$head" = 1 ]; then\n'
        '  printf "content-length: %s\\r\\n" "$(printf "%s\\n" "$body" | wc -c | tr -d " ")"\n'
        "  exit 0\n"
        "fi\n"
        'if [ -n "$out" ]; then mkdir -p "$(dirname "$out")"; printf "%s\n" "$body" > "$out"\n'
        'else printf "%s\n" "$body"; fi\n'
    )
    script.write_bytes(body.encode())  # LF endings: a CRLF shebang breaks bash on Windows
    script.chmod(0o755)


def _run(tmp_path, *args):
    log = tmp_path / "curl.log"
    _fake_curl(tmp_path / "bin", log)
    env = {
        **os.environ,
        "SHIM_DIR": _msys_path(tmp_path / "bin"),
        "EPISTEME_ACTOR": "episteme_sys_admin",
        "EPISTEME_DATA_ROOT": str(tmp_path),
        "EPISTEME_RAW_ROOT": str(tmp_path / "01_raw"),
        "PGDATABASE": "episteme_test",
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
    return tmp_path / "01_raw" / "cdisc_bc"


def test_fetches_files_provenance_and_licence(tmp_path):
    proc, calls = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    exp = _dest(tmp_path) / "export"
    assert (exp / "bc_latest.csv").is_file() and (exp / "sdtm_latest.csv").is_file()
    assert not (exp / "archive").exists()  # dir entries are not files
    prov = (_dest(tmp_path) / "PROVENANCE.txt").read_text(encoding="utf-8")
    assert f"commit_sha: {SHA}" in prov  # top-level sha, not the nested one
    assert "source_repo: https://github.com/cdisc-org/COSMoS" in prov
    assert "not stated for export/ data" in prov
    assert "CC-BY-4.0 (repository content)" not in prov
    assert "retrieved_at: 20" in prov and prov.count("Z") >= 1
    assert (_dest(tmp_path) / "LICENSE").read_text(encoding="utf-8").startswith("licence text")


def test_max_files_fetches_only_one_data_file(tmp_path):
    proc, _ = _run(tmp_path, "--max-files", "1")
    assert proc.returncode == 0, proc.stderr[-2000:]
    exp = _dest(tmp_path) / "export"
    assert (exp / "bc_latest.csv").is_file()
    assert not (exp / "sdtm_latest.csv").exists()
    assert (_dest(tmp_path) / "PROVENANCE.txt").is_file()


def test_dry_run_fetches_nothing_and_writes_no_provenance(tmp_path):
    proc, calls = _run(tmp_path, "--dry-run")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stderr.count("DRY: would fetch") == 3  # 2 data files + LICENSE
    assert not _dest(tmp_path).exists()


def test_force_dry_run_deletes_nothing(tmp_path):
    proc, _ = _run(tmp_path)
    assert proc.returncode == 0
    proc, _ = _run(tmp_path, "--dry-run", "--force")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert (_dest(tmp_path) / "export" / "bc_latest.csv").is_file()


def test_up_to_date_run_does_not_rewrite_provenance(tmp_path):
    proc, _ = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    prov = _dest(tmp_path) / "PROVENANCE.txt"
    prov.write_text("sentinel\n", encoding="utf-8")
    proc, _ = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "up to date" in proc.stderr
    assert prov.read_text(encoding="utf-8") == "sentinel\n"


def test_yaml_positional_dies_unknown_mode(tmp_path):
    proc, _ = _run(tmp_path, "yaml")
    assert proc.returncode != 0
    assert "unknown mode 'yaml'" in proc.stderr
    assert not _dest(tmp_path).exists()
