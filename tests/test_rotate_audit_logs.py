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


def test_default_ops_dir_resolves_via_episteme_data_root(tmp_path):
    # No positional ops_dir: the script must resolve the same default
    # episteme.audit_trail._mirror_dir() does (processed_root falls back to
    # <EPISTEME_DATA_ROOT>/02_processed when EPISTEME_PROCESSED_ROOT is
    # unset -- config.py, not a value the script may hardcode/reimplement).
    #
    # This must NOT depend on the operator's real .env: popping
    # EPISTEME_PROCESSED_ROOT from this subprocess's env dict does not stop
    # the script's own load_dotenv (scripts/data/_lib/common.sh) from
    # re-filling it from <repo-root>/.env if that file defines it -- and
    # common.sh resolves <repo-root> from ITS OWN on-disk location
    # (BASH_SOURCE), not from cwd/HOME, so no cwd/HOME arrangement run from
    # the real script path can hide a real .env from it. Instead, run copies
    # of rotate_audit_logs.sh + _lib/common.sh from a directory with no
    # pyproject.toml ancestor: _common_repo_root() then fails to find a repo
    # root at all, and load_dotenv() takes its documented "no pyproject.toml
    # ancestor, skipping" no-op branch -- guaranteed .env-independent
    # without ever reading .env. PYTHON is pointed at the real interpreter
    # (sys.executable) so the script's `_mirror_dir()` call still resolves
    # against the real, installed episteme package (editable install, works
    # from any cwd) rather than a copied one.
    #
    # The `_mirror_dir()` subprocess's own .env lookup is covered too:
    # episteme.config._find_project_dotenv() searches from Path.cwd() (not
    # __file__), and `cwd=str(isolated)` below (also outside any
    # pyproject.toml ancestor) makes it return None -- confirmed directly by
    # running `_find_project_dotenv()` with this exact cwd arrangement --
    # so get_settings() cannot load a real .env at that layer either.
    import os
    import shutil
    import sys
    import time

    data_root = tmp_path / "data_root"
    ops_dir = data_root / "02_processed" / "_ops" / "_audit"
    ops_dir.mkdir(parents=True)
    old = ops_dir / "audit-20260101.jsonl"
    old.write_text('{"a": 1}\n', encoding="utf-8")
    old_ts = time.time() - 40 * 86400
    os.utime(old, (old_ts, old_ts))

    isolated = tmp_path / "isolated_scripts"
    (isolated / "_lib").mkdir(parents=True)
    shutil.copy(SCRIPT, isolated / SCRIPT.name)
    shutil.copy(REPO / "scripts" / "data" / "_lib" / "common.sh", isolated / "_lib" / "common.sh")

    env = dict(os.environ)
    env.pop("EPISTEME_PROCESSED_ROOT", None)
    env["EPISTEME_DATA_ROOT"] = str(data_root)
    env["PYTHON"] = sys.executable

    proc = subprocess.run(
        [_bash(), str(isolated / SCRIPT.name)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=str(isolated),
    )
    assert "load_dotenv: no pyproject.toml ancestor, skipping" in proc.stderr, proc.stderr
    assert proc.returncode == 0, proc.stderr
    assert not old.exists()
    assert (ops_dir / "audit-20260101.jsonl.gz").is_file()


def _chattr_available() -> bool:
    import shutil

    return shutil.which("chattr") is not None


def _append_only_actually_enforced(probe_dir) -> bool:
    """True only if chattr +a on THIS filesystem really blocks unlink.

    Some sandboxes (e.g. an NTFS-backed Git-for-Windows bash) have a chattr
    binary that accepts `+a` and reports success, but the filesystem does
    not enforce it -- `rm`/unlink still succeeds. Only trust the probe, not
    chattr's own exit code.
    """
    import subprocess as sp

    probe = probe_dir / "probe.txt"
    probe.write_text("x", encoding="utf-8")
    sp.run(["chattr", "+a", str(probe)], capture_output=True, text=True)
    rc = sp.run(["rm", "-f", str(probe)], capture_output=True, text=True).returncode
    sp.run(["chattr", "-a", str(probe)], capture_output=True, text=True)
    if probe.exists():
        probe.unlink()
    return rc != 0


def test_chattrd_eligible_file_still_rotates(tmp_path):
    # Critical fix: chattr +a (applied to a prior day's file once it becomes
    # "today's" file) blocks the unlink() gzip needs -- once that file ages
    # past 30 days, rotation must still succeed (chattr -a before gzip), and
    # must not be `die`'d if it somehow still failed.
    if not _chattr_available():
        pytest.skip("no chattr on this system")
    probe_dir = tmp_path / "_probe"
    probe_dir.mkdir()
    if not _append_only_actually_enforced(probe_dir):
        pytest.skip("chattr +a is not enforced on this filesystem/sandbox (no CAP_LINUX_IMMUTABLE)")

    import os
    import time

    audit_dir = tmp_path / "_audit"
    audit_dir.mkdir()
    old = audit_dir / "audit-20260101.jsonl"
    old.write_text('{"a": 1}\n', encoding="utf-8")
    old_ts = time.time() - 40 * 86400
    os.utime(old, (old_ts, old_ts))
    subprocess.run(["chattr", "+a", str(old)], capture_output=True, text=True, check=True)

    proc = _run(audit_dir)
    assert proc.returncode == 0, proc.stderr
    assert not old.exists()
    assert (audit_dir / "audit-20260101.jsonl.gz").is_file()
