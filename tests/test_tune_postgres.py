"""scripts/data/db/tune_postgres.sh: `check` compares pg_settings with the
recommended bulk-load settings and fails on any difference, pending restart or
role/database override; `apply` validates, sets them with ALTER SYSTEM as the
superuser, reloads, and confirms each value is active or waiting for a restart.
It never puts a password on the command line or in its output.

A fake `psql` on PATH logs each call's arguments and answers each query kind
from an environment variable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "scripts" / "data"

FAKE_PSQL = r"""#!/usr/bin/env bash
echo "T=${PGCONNECT_TIMEOUT:-unset} $*" >> "$FAKE_PSQL_LOG"
case "$*" in
    *enumvals*) printf '%s\n' $FAKE_VALIDATION ;;
    *pg_reload_conf*) echo "${FAKE_RELOAD:-t}" ;;
    *pg_file_settings*) printf '%s\n' $FAKE_UNAPPLIED ;;
    *pg_db_role_setting*) printf '%s\n' $FAKE_OVERRIDES ;;
    *pg_settings*) printf '%s\n' $FAKE_ROWS ;;
esac
exit 0
"""

RECOMMENDED = {
    "checkpoint_timeout": "30min",
    "effective_cache_size": "8GB",
    "maintenance_work_mem": "1GB",
    "max_wal_size": "16GB",
    "min_wal_size": "2GB",
    "shared_buffers": "2GB",
    "wal_compression": "lz4",
}
SECRET = "s3cret-pw"
SU = {"POSTGRES_SUPERUSER": "pgsu", "POSTGRES_SUPERUSER_PASSWORD": SECRET}


def _bash() -> str:
    for c in (r"C:\Program Files\Git\bin\bash.exe", "/usr/bin/bash", "/bin/bash"):
        if Path(c).exists():
            return c
    pytest.skip("no POSIX bash")


def _rows(overrides: dict[str, str] | None = None, pending: str = "f") -> str:
    vals = {**RECOMMENDED, **(overrides or {})}
    return " ".join(f"{k}|{v}|{pending}" for k, v in sorted(vals.items()))


def _valid() -> str:
    return " ".join(f"{k}|ok" for k in sorted(RECOMMENDED))


def _run(
    tmp_path: Path,
    *args: str,
    rows: str = "",
    unapplied: str = "",
    overrides: str = "",
    validation: str | None = None,
    extra: dict[str, str] | None = None,
):
    root = tmp_path / "repo"
    shutil.copytree(DATA / "_lib", root / "scripts" / "data" / "_lib")
    (root / "scripts" / "data" / "db").mkdir(parents=True)
    shutil.copy(DATA / "db" / "tune_postgres.sh", root / "scripts" / "data" / "db")
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    fake = tmp_path / "psql"
    fake.write_bytes(FAKE_PSQL.encode("utf-8"))
    fake.chmod(0o755)
    log = tmp_path / "psql.log"
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("EPISTEME_", "POSTGRES_", "PG"))
    }
    env.update(
        {
            "PSQL": fake.as_posix(),
            "PGHOST": "localhost",
            "PGPORT": "5433",
            "EPISTEME_SYS_ADMIN_PASSWORD": SECRET,
            "FAKE_PSQL_LOG": log.as_posix(),
            "FAKE_ROWS": rows,
            "FAKE_UNAPPLIED": unapplied,
            "FAKE_OVERRIDES": overrides,
            "FAKE_VALIDATION": _valid() if validation is None else validation,
            **(extra or {}),
        }
    )
    script = (root / "scripts" / "data" / "db" / "tune_postgres.sh").as_posix()
    proc = subprocess.run(
        [_bash(), script, *args], env=env, capture_output=True, text=True, timeout=60
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


def test_check_passes_when_all_settings_match(tmp_path):
    proc, calls = _run(tmp_path, "check", rows=_rows())
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DIFFERS" not in proc.stdout
    assert calls and all("-U episteme_sys_admin" in c for c in calls)


def test_check_fails_and_names_the_differing_setting(tmp_path):
    proc, _ = _run(tmp_path, "check", rows=_rows({"shared_buffers": "128MB"}))
    assert proc.returncode == 1
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("shared_buffers"))
    assert "128MB" in line and "2GB" in line and "DIFFERS" in line


def test_check_fails_when_a_restart_is_pending(tmp_path):
    # the running value matches, but the next start may bring another one
    proc, _ = _run(tmp_path, "check", rows=_rows(pending="t"))
    assert proc.returncode == 1
    assert "restart pending" in proc.stdout


def test_check_fails_on_a_role_or_database_override(tmp_path):
    proc, _ = _run(
        tmp_path,
        "check",
        rows=_rows(),
        overrides="episteme|episteme_app|maintenance_work_mem=64MB",
    )
    assert proc.returncode == 1
    assert "OVERRIDE database=episteme role=episteme_app maintenance_work_mem=64MB" in proc.stdout


def test_connections_have_a_bounded_timeout(tmp_path):
    proc, calls = _run(tmp_path, "check", rows=_rows())
    assert proc.returncode == 0
    assert calls and all(c.startswith("T=10 ") for c in calls)


def test_connect_timeout_can_be_overridden(tmp_path):
    proc, calls = _run(tmp_path, "check", rows=_rows(), extra={"PGCONNECT_TIMEOUT": "3"})
    assert proc.returncode == 0
    assert calls and all(c.startswith("T=3 ") for c in calls)


def test_apply_sets_every_setting_as_superuser_without_leaking_password(tmp_path):
    proc, calls = _run(
        tmp_path,
        "apply",
        # pending_restart stays "f" (as on Windows); pg_file_settings reports it
        rows=_rows({"shared_buffers": "128MB"}),
        unapplied="shared_buffers",
        extra=SU,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    alters = [c for c in calls if "ALTER SYSTEM SET" in c]
    assert len(alters) == len(RECOMMENDED)
    for name, value in RECOMMENDED.items():
        assert any(f"ALTER SYSTEM SET {name} = '{value}'" in c for c in alters), name
    assert all("-U pgsu" in c and "-d postgres" in c for c in calls)
    assert any("pg_reload_conf" in c for c in calls)
    out = proc.stderr + proc.stdout
    assert "restart the PostgreSQL service to apply: shared_buffers" in out
    assert "all settings active" not in out
    assert SECRET not in out + "\n".join(calls)


def test_apply_reports_all_active_when_nothing_is_unapplied(tmp_path):
    proc, _ = _run(tmp_path, "apply", rows=_rows(), extra=SU)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all settings active" in proc.stderr + proc.stdout


def test_apply_validates_before_writing_anything(tmp_path):
    proc, calls = _run(
        tmp_path,
        "apply",
        rows=_rows(),
        validation="wal_compression|value_not_supported_by_this_server",
        extra=SU,
    )
    assert proc.returncode != 0
    assert not any("ALTER SYSTEM" in c for c in calls)
    assert "wal_compression" in proc.stdout + proc.stderr


def test_apply_fails_when_reload_is_refused(tmp_path):
    proc, _ = _run(tmp_path, "apply", rows=_rows(), extra={**SU, "FAKE_RELOAD": "f"})
    assert proc.returncode != 0
    assert "pg_reload_conf() returned 'f'" in proc.stdout + proc.stderr


def test_apply_fails_when_a_value_never_becomes_active(tmp_path):
    proc, _ = _run(
        tmp_path,
        "apply",
        rows=_rows({"max_wal_size": "1GB"}),
        extra={**SU, "EPISTEME_TUNE_VERIFY_TRIES": "2"},
    )
    assert proc.returncode != 0
    out = proc.stdout + proc.stderr
    assert "not active after reload: max_wal_size=1GB" in out
    assert "all settings active" not in out


def test_apply_without_superuser_password_changes_nothing(tmp_path):
    proc, calls = _run(tmp_path, "apply", rows=_rows())
    assert proc.returncode != 0
    assert calls == []
    assert "POSTGRES_SUPERUSER_PASSWORD" in proc.stdout + proc.stderr


def test_unknown_mode_is_rejected(tmp_path):
    proc, calls = _run(tmp_path, "tune-everything")
    assert proc.returncode != 0
    assert calls == []
