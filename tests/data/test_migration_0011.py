"""Migration 0011 adds label_section.ordinal and label_section.product (pg: episteme_test only)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.pg

MIGRATIONS = Path(__file__).resolve().parents[2] / "src/episteme/data/db/migrations"
_COLUMNS = (
    "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
    "WHERE table_schema = 'episteme' AND table_name = 'label_section'"
)


def _apply(conn, name):
    with conn.cursor() as cur:
        cur.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
    conn.commit()


def test_migration_0011_is_idempotent(pg_conn):
    _apply(pg_conn, "0010_regulatory_sources.sql")
    _apply(pg_conn, "0011_label_section_ordinal.sql")
    _apply(pg_conn, "0011_label_section_ordinal.sql")


def test_label_section_has_ordinal_and_product(pg_conn):
    _apply(pg_conn, "0010_regulatory_sources.sql")
    _apply(pg_conn, "0011_label_section_ordinal.sql")
    with pg_conn.cursor() as cur:
        cur.execute(_COLUMNS)
        cols = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    assert cols["ordinal"] == ("integer", "YES")
    assert cols["product"] == ("text", "YES")
