import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "scripts" / "data" / "openalex" / "download_openalex.sh"


def _bash() -> str:
    for c in (r"C:\Program Files\Git\bin\bash.exe", "/usr/bin/bash", "/bin/bash"):
        if Path(c).exists():
            return c
    pytest.skip("no POSIX bash")


P = "data/jsonl/works/"
LISTING = [  # deliberately unsorted; manifest first as in the real bucket
    P + "manifest.json",
    P + "updated_date=2026-05-03/part_0000.gz",
    P + "updated_date=2026-05-01/part_0000.gz",
    P + "zz_manifest.json",
    P + "updated_date=2026-05-02/part_0000.gz",
]


def _fake_aws(bindir: Path, log: Path, keys: list[str]) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    listing = bindir / "listing.txt"
    listing.write_text(
        "".join(f"2026-06-01 00:00:00        100 {k}\n" for k in keys), encoding="utf-8"
    )
    script = bindir / "aws"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log.as_posix()}"\n'
        'if [ "$2" = "ls" ]; then\n'
        f'  cat "{listing.as_posix()}"\n'
        'elif [ "$2" = "cp" ]; then\n'
        '  mkdir -p "$(dirname "$5")"; echo data > "$5"\n'
        "fi\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def _run(tmp_path, *args, keys=None):
    log = tmp_path / "aws.log"
    _fake_aws(tmp_path / "bin", log, LISTING if keys is None else keys)
    env = {
        **os.environ,
        "PATH": f"{(tmp_path / 'bin').as_posix()}{os.pathsep}{os.environ['PATH']}",
        "EPISTEME_ACTOR": "episteme_sys_admin",
        "EPISTEME_DATA_ROOT": str(tmp_path),
        "EPISTEME_RAW_ROOT": str(
            tmp_path / "01_raw"
        ),  # a leaked value from another test must not win
        "PGDATABASE": "episteme_test",
    }
    proc = subprocess.run(
        [_bash(), str(WRAPPER), *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


def _dest(tmp_path) -> Path:
    return tmp_path / "01_raw" / "openalex" / "data" / "jsonl" / "works"


def test_max_files_fetches_exactly_n_objects_in_key_order(tmp_path):
    proc, calls = _run(tmp_path, "--max-files", "2")
    assert proc.returncode == 0, proc.stderr[-2000:]
    cps = [c for c in calls if " cp " in f" {c} "]
    assert len(cps) == 2
    assert "updated_date=2026-05-01" in cps[0] and "updated_date=2026-05-02" in cps[1]
    assert not any(" sync " in f" {c} " for c in calls)
    dest = _dest(tmp_path)
    assert (dest / "updated_date=2026-05-01" / "part_0000.gz").is_file()
    assert (dest / "updated_date=2026-05-02" / "part_0000.gz").is_file()
    assert not (dest / "manifest.json").exists()
    assert (dest / "sync_mode.txt").is_file()


def test_only_exact_manifest_basename_is_excluded(tmp_path):
    # N large enough to take everything: zz_manifest.json must survive, manifest.json must not.
    proc, calls = _run(tmp_path, "--max-files", "10")
    assert proc.returncode == 0, proc.stderr[-2000:]
    cps = [c for c in calls if " cp " in f" {c} "]
    assert len(cps) == 4
    assert any("zz_manifest.json" in c for c in cps)
    assert not any(c.split()[-2].endswith("/manifest.json") for c in cps)
    assert (_dest(tmp_path) / "zz_manifest.json").is_file()


def test_dry_run_lists_exactly_n_and_fetches_nothing(tmp_path):
    proc, calls = _run(tmp_path, "--dry-run", "--max-files", "2")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stderr.count("DRY: would fetch") == 2
    assert not any(" cp " in f" {c} " for c in calls)
    assert not _dest(tmp_path).exists()


@pytest.mark.parametrize("bad", ["abc", "-1", "1.5"])
def test_invalid_max_files_dies_without_fetching(tmp_path, bad):
    proc, calls = _run(tmp_path, "--max-files", bad)
    assert proc.returncode != 0
    assert "--max-files must be a non-negative integer" in proc.stderr
    assert not any(" sync " in f" {c} " or " cp " in f" {c} " for c in calls)


def test_empty_listing_fails_and_writes_no_stamp(tmp_path):
    proc, calls = _run(tmp_path, "--max-files", "2", keys=[])
    assert proc.returncode != 0
    assert "no objects" in proc.stderr
    assert not (_dest(tmp_path) / "sync_mode.txt").exists()
    assert not (_dest(tmp_path) / "last_sync_utc.txt").exists()


@pytest.mark.skipif(
    shutil.which("s5cmd") is not None, reason="s3_sync prefers s5cmd when installed"
)
@pytest.mark.parametrize("args", [[], ["--max-files", "0"]])
def test_no_max_files_or_zero_still_uses_sync(tmp_path, args):
    proc, calls = _run(tmp_path, *args)
    assert any(" sync " in f" {c} " for c in calls)
