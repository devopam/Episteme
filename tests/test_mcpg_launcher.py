"""scripts/mcpg-episteme.sh: the everyday server connects as episteme_sys_admin
with ALTER SYSTEM off; `--admin` connects as the superuser with ALTER SYSTEM on
and an allowlist. Passwords are percent-encoded in the URL and never printed.

The script is copied into a temp repo (no .env there) and a fake `mcpg` on PATH
prints the variables it would have received.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FAKE_MCPG = r"""#!/usr/bin/env bash
for v in MCPG_DATABASE_URL MCPG_SECONDARY_DATABASE_URLS MCPG_ALLOW_ALTER_SYSTEM \
         MCPG_ALTER_SYSTEM_ALLOWLIST MCPG_ACCESS_MODE MCPG_TRANSPORT; do
    echo "$v=${!v-<unset>}"
done
"""
SECRET = "p@ss:w/rd%#"
ENCODED = "p%40ss%3Aw%2Frd%25%23"


def _bash() -> str:
    for c in (r"C:\Program Files\Git\bin\bash.exe", "/usr/bin/bash", "/bin/bash"):
        if Path(c).exists():
            return c
    pytest.skip("no POSIX bash")


def _run(tmp_path: Path, args: list[str], env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "mcpg-episteme.sh", repo / "scripts" / "mcpg-episteme.sh")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "mcpg"
    fake.write_bytes(FAKE_MCPG.encode())
    fake.chmod(0o755)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("MCPG_", "POSTGRES_", "EPISTEME_", "PG"))
    }
    env.update(env_extra)
    env["PATH"] = f"{bindir.as_posix()}{os.pathsep}{env.get('PATH', '')}"
    script = (repo / "scripts" / "mcpg-episteme.sh").as_posix()
    # Git Bash needs the fake's directory as a POSIX path; elsewhere as_posix() already is one.
    bin_posix = f'$(cygpath -u "{bindir}" 2>/dev/null || echo "{bindir.as_posix()}")'
    cmd = f'PATH="{bin_posix}:$PATH" exec bash "{script}" {" ".join(args)}'
    return subprocess.run([_bash(), "-c", cmd], env=env, capture_output=True, text=True, timeout=30)


def _vars(out: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def test_everyday_server_is_sys_admin_with_alter_system_off(tmp_path):
    r = _run(
        tmp_path,
        ["episteme"],
        {"EPISTEME_SYS_ADMIN_PASSWORD": SECRET, "MCPG_ALLOW_ALTER_SYSTEM": "true"},
    )
    assert r.returncode == 0, r.stderr
    v = _vars(r.stdout)
    assert (
        v["MCPG_DATABASE_URL"]
        == f"postgresql://episteme_sys_admin:{ENCODED}@localhost:5433/episteme"
    )
    assert v["MCPG_SECONDARY_DATABASE_URLS"].startswith(
        "episteme_test=postgresql://episteme_sys_admin:"
    )
    assert v["MCPG_ALLOW_ALTER_SYSTEM"] == "false"
    assert v["MCPG_TRANSPORT"] == "stdio"


def test_admin_server_is_superuser_with_alter_system_allowlist(tmp_path):
    r = _run(
        tmp_path,
        ["episteme", "--admin"],
        {
            "EPISTEME_SYS_ADMIN_PASSWORD": "unused",
            "POSTGRES_SUPERUSER": "pgsu",
            "POSTGRES_SUPERUSER_PASSWORD": SECRET,
            "MCPG_ACCESS_MODE": "read-only",
            "MCPG_SECONDARY_DATABASE_URLS": "x=postgresql://someone@h/x",
        },
    )
    assert r.returncode == 0, r.stderr
    v = _vars(r.stdout)
    assert v["MCPG_DATABASE_URL"] == f"postgresql://pgsu:{ENCODED}@localhost:5433/episteme"
    assert v["MCPG_SECONDARY_DATABASE_URLS"] == "<unset>"
    assert v["MCPG_ALLOW_ALTER_SYSTEM"] == "true"
    assert v["MCPG_ACCESS_MODE"] == "unrestricted"
    allow = v["MCPG_ALTER_SYSTEM_ALLOWLIST"].split(",")
    expected = {"shared_buffers", "max_wal_size", "checkpoint_timeout", "maintenance_work_mem"}
    assert expected <= set(allow)


def test_admin_superuser_defaults_to_postgres(tmp_path):
    r = _run(tmp_path, ["episteme", "--admin"], {"POSTGRES_SUPERUSER_PASSWORD": "pw"})
    assert r.returncode == 0, r.stderr
    assert _vars(r.stdout)["MCPG_DATABASE_URL"].startswith("postgresql://postgres:pw@")


def test_admin_without_superuser_password_fails_without_printing_secrets(tmp_path):
    r = _run(tmp_path, ["episteme", "--admin"], {"EPISTEME_SYS_ADMIN_PASSWORD": SECRET})
    assert r.returncode != 0
    assert "POSTGRES_SUPERUSER_PASSWORD not set" in r.stderr
    assert "MCPG_DATABASE_URL" not in r.stdout
    assert SECRET not in r.stdout + r.stderr


def test_unknown_option_is_rejected(tmp_path):
    r = _run(tmp_path, ["episteme", "--superuser"], {"EPISTEME_SYS_ADMIN_PASSWORD": "pw"})
    assert r.returncode == 2
    assert "unknown option" in r.stderr
