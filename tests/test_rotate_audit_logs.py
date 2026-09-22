import gzip
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "data" / "rotate_audit_logs.sh"


def _bash() -> str:
    for c in (r"C:\Program Files\Git\bin\bash.exe", "/usr/bin/bash", "/bin/bash"):
        if Path(c).exists():
            return c
    pytest.skip("no POSIX bash")


def _run(ops_dir):
    return subprocess.run(
        [_bash(), str(SCRIPT), str(ops_dir)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_gzips_files_older_than_30_days_leaves_recent_alone(tmp_path):
    import os
    import time

    audit_dir = tmp_path / "_audit"
    audit_dir.mkdir()
    old = audit_dir / "audit-20260101.jsonl"
    old.write_text('{"a": 1}\n', encoding="utf-8")
    recent = audit_dir / "audit-20260921.jsonl"
    recent.write_text('{"b": 2}\n', encoding="utf-8")
    old_ts = time.time() - 40 * 86400
    os.utime(old, (old_ts, old_ts))

    proc = _run(audit_dir)
    assert proc.returncode == 0, proc.stderr

    assert not old.exists()
    assert (audit_dir / "audit-20260101.jsonl.gz").is_file()
    with gzip.open(audit_dir / "audit-20260101.jsonl.gz", "rt", encoding="utf-8") as f:
        assert f.read() == '{"a": 1}\n'
    assert recent.is_file()  # untouched
    assert not (audit_dir / "audit-20260921.jsonl.gz").exists()


def test_idempotent_on_already_rotated_file(tmp_path):
    audit_dir = tmp_path / "_audit"
    audit_dir.mkdir()
    (audit_dir / "audit-20260101.jsonl.gz").write_bytes(b"already-gzipped")

    proc = _run(audit_dir)
    assert proc.returncode == 0, proc.stderr
    assert (audit_dir / "audit-20260101.jsonl.gz").read_bytes() == b"already-gzipped"


def test_missing_dir_is_a_noop_not_an_error(tmp_path):
    proc = _run(tmp_path / "does_not_exist")
    assert proc.returncode == 0, proc.stderr
