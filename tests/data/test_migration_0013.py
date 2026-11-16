"""Migration 0013 and one reference-code load (pg: episteme_test only)."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from episteme.config import get_settings
from episteme.data.refmeta.load import load_release
from episteme.data.refmeta.model import CodeRelease, CodeRow
from episteme.reference.codes import lookup_code

pytestmark = pytest.mark.pg

_MIGRATION = (
    Path(__file__).resolve().parents[2] / "src/episteme/data/db/migrations/0013_reference_codes.sql"
)
_SYSTEM = "zz_refmeta"


def _apply(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(_MIGRATION.read_text(encoding="utf-8"))
    conn.commit()


def _release(display: str) -> CodeRelease:
    return CodeRelease(
        system_key=_SYSTEM,
        release_id="r1",
        version="1",
        licence_notice="test notice, kept with the code",
        source_file="fixture",
        codes=(CodeRow(code="JP", display=display, extra={"alpha_3": "JPN"}),),
    )


def test_migration_0013_is_idempotent_and_refuses_an_empty_notice(pg_conn):
    _apply(pg_conn)
    _apply(pg_conn)
    with pg_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO episteme.code_system "
                "(system_key, release_id, licence_notice, source_file) "
                "VALUES (%s, 'blank', '   ', 'f')",
                (_SYSTEM,),
            )
    pg_conn.rollback()


def test_load_replace_view_and_lookup(pg_conn, monkeypatch, tmp_path):
    monkeypatch.setenv("EPISTEME_ACTOR", "pytest")
    get_settings.cache_clear()
    _apply(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM episteme.code WHERE system_key = %s", (_SYSTEM,))
        cur.execute("DELETE FROM episteme.code_system WHERE system_key = %s", (_SYSTEM,))
    pg_conn.commit()
    assert (
        load_release(pg_conn, _release("Japan"), processed_root=tmp_path, force=False, reason=None)
        == "loaded"
    )
    assert (
        load_release(pg_conn, _release("Japan"), processed_root=tmp_path, force=False, reason=None)
        == "skipped"
    )
    assert (
        load_release(
            pg_conn,
            _release("Japan (updated)"),
            processed_root=tmp_path,
            force=True,
            reason="test reload",
        )
        == "replaced"
    )
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT display, licence_notice FROM episteme.code_current "
            "WHERE system_key = %s AND code = 'JP'",
            (_SYSTEM,),
        )
        display, notice = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM episteme.code_system WHERE system_key = %s AND is_latest",
            (_SYSTEM,),
        )
        latest = cur.fetchone()[0]
    assert display == "Japan (updated)"
    assert notice == "test notice, kept with the code"
    assert latest == 1
    found = lookup_code(pg_conn, _SYSTEM, "JP", tenant="t", caller="c")
    assert found.coverage.matched == 1
    assert found.claims[0].value == "Japan (updated)"
    assert "test notice" in found.claims[0].text
    missed = lookup_code(pg_conn, _SYSTEM, "ZZ", tenant="t", caller="c")
    assert missed.coverage.matched == 0
    assert missed.claims == ()
