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


def _fake_aws(bindir: Path, log: Path) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / "aws"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log.as_posix()}"\n'
        'if [ "$2" = "ls" ]; then\n'
        "  printf '2026-06-01 00:00:00        100 "
        "data/jsonl/works/updated_date=2026-05-01/part_0000.gz\\n'\n"
        "  printf '2026-06-01 00:00:00        100 "
        "data/jsonl/works/updated_date=2026-05-02/part_0000.gz\\n'\n"
        "  printf '2026-06-01 00:00:00        100 "
        "data/jsonl/works/updated_date=2026-05-03/part_0000.gz\\n'\n"
        'elif [ "$2" = "cp" ]; then\n'
        '  mkdir -p "$(dirname "$5")"; echo data > "$5"\n'
        "fi\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def _run(tmp_path, *args):
    log = tmp_path / "aws.log"
    _fake_aws(tmp_path / "bin", log)
    env = {
        **os.environ,
        "PATH": f"{(tmp_path / 'bin').as_posix()}{os.pathsep}{os.environ['PATH']}",
        "EPISTEME_ACTOR": "episteme_sys_admin",
        "EPISTEME_DATA_ROOT": str(tmp_path),
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


def test_max_files_fetches_exactly_n_objects_in_key_order(tmp_path):
    proc, calls = _run(tmp_path, "--max-files", "2")
    assert proc.returncode == 0, proc.stderr[-2000:]
    cps = [c for c in calls if " cp " in f" {c} "]
    assert len(cps) == 2
    assert "updated_date=2026-05-01" in cps[0] and "updated_date=2026-05-02" in cps[1]
    assert not any(" sync " in f" {c} " for c in calls)


@pytest.mark.skipif(
    shutil.which("s5cmd") is not None, reason="s3_sync prefers s5cmd when installed"
)
def test_no_max_files_still_uses_sync(tmp_path):
    proc, calls = _run(tmp_path)
    assert any(" sync " in f" {c} " for c in calls)
