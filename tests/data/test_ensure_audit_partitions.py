"""pg-marked test for scripts/data/db/ensure_audit_partitions.sh.

Shells out to the real script against episteme_test (via TEST_PG_DSN /
EPISTEME_SYS_ADMIN_PASSWORD, same as every other pg-marked test in this
directory) and asserts on the resulting catalog state through ``pg_conn``.

Connection parameters for the subprocess come from parsing TEST_PG_DSN with
psycopg.conninfo.conninfo_to_dict -- pg_conn (tests/data/conftest.py) does not
expose a parsed-parameters API, so this mirrors what that fixture itself does
to validate TEST_PG_DSN, rather than assuming one.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "data" / "db" / "ensure_audit_partitions.sh"

pytestmark = pytest.mark.pg


def _setup_schema(conn):
    # Same helper (duplicated per-file, matching this directory's convention
    # -- see tests/data/test_audit_trail.py etc.) that rebuilds episteme from
    # scratch so this test is self-contained regardless of run order/history.
    with (
        conn.cursor() as cur,
        open("src/episteme/data/db/extensions.sql") as ext,
        open("src/episteme/data/db/schema.sql") as sch,
    ):
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute(ext.read())
        cur.execute(sch.read())
    conn.commit()


def _bash() -> str:
    for c in (r"C:\Program Files\Git\bin\bash.exe", "/usr/bin/bash", "/bin/bash"):
        if Path(c).exists():
            return c
    pytest.skip("no POSIX bash")
    raise AssertionError("unreachable")  # pragma: no cover - pytest.skip raises


def _run_script(months_ahead: int) -> subprocess.CompletedProcess[str]:
    dsn_parts = psycopg.conninfo.conninfo_to_dict(os.environ["TEST_PG_DSN"])
    env = {
        "PGHOST": str(dsn_parts.get("host", "localhost")),
        "PGPORT": str(dsn_parts.get("port", "5433")),
        "PGDATABASE": "episteme_test",
        "EPISTEME_SYS_ADMIN_PASSWORD": os.environ["EPISTEME_SYS_ADMIN_PASSWORD"],
        "PATH": os.environ["PATH"],
    }
    # psql.exe on Windows needs a few more ambient vars than PATH alone to
    # initialize cleanly; pass them through when present rather than risk an
    # opaque connection failure that looks like a credentials problem.
    for k in ("SYSTEMROOT", "TEMP", "TMP", "COMSPEC"):
        if k in os.environ:
            env[k] = os.environ[k]
    return subprocess.run(
        [_bash(), str(SCRIPT), "episteme_test", str(months_ahead)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _expected_partition_names(months_ahead: int) -> list[str]:
    now = datetime.now(timezone.utc)
    names = []
    y, m = now.year, now.month
    for i in range(months_ahead):
        total = (m - 1) + i
        yy = y + total // 12
        mm = total % 12 + 1
        names.append(f"_audit_{yy:04d}{mm:02d}")
    return names


def _audit_partition_names(pg_conn) -> set[str]:
    with pg_conn.cursor() as cur:
        cur.execute(
            "select c.relname from pg_class c "
            "join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'episteme' and c.relname ~ '^_audit_2[0-9]{5}$'"
        )
        return {r[0] for r in cur.fetchall()}


def test_creates_future_partitions_and_is_idempotent(pg_conn):
    _setup_schema(pg_conn)
    proc = _run_script(8)
    assert proc.returncode == 0, proc.stderr

    expected = set(_expected_partition_names(8))
    names = _audit_partition_names(pg_conn)
    missing = expected - names
    assert not missing, f"expected monthly _audit partitions not found: {sorted(missing)}"

    # Idempotent: running again with the same months_ahead must not error and
    # must not duplicate/alter the set of partitions.
    proc2 = _run_script(8)
    assert proc2.returncode == 0, proc2.stderr
    assert _audit_partition_names(pg_conn) == names


def test_new_partition_denies_update_delete_to_episteme_app(pg_conn):
    # Ensure at least one partition beyond the schema.sql-created
    # 202609/202610 exists, then confirm episteme_app cannot UPDATE or DELETE
    # it: the REVOKE must be re-applied per new partition, since
    # ALTER DEFAULT PRIVILEGES only grants (never revokes) on future tables.
    _setup_schema(pg_conn)
    proc = _run_script(8)
    assert proc.returncode == 0, proc.stderr

    with pg_conn.cursor() as cur:
        cur.execute(
            "select has_table_privilege('episteme_app', c.oid, 'UPDATE') as can_update, "
            "       has_table_privilege('episteme_app', c.oid, 'DELETE') as can_delete, "
            "       c.relname "
            "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'episteme' and c.relname ~ '^_audit_2[0-9]{5}$' "
            "  and c.relname not in ('_audit_202609', '_audit_202610')"
        )
        rows = cur.fetchall()
    assert rows, "expected at least one newly created partition beyond the schema.sql defaults"
    for can_update, can_delete, name in rows:
        assert not can_update, f"{name} still grants UPDATE to episteme_app"
        assert not can_delete, f"{name} still grants DELETE to episteme_app"
