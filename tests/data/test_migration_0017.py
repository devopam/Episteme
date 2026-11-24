"""Migration 0017 (API no timeouts): seeds equal config, one transaction, gate marker,
grants (non-pg); idempotent apply after 0016 (pg: episteme_test only)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from episteme import config
from episteme.ops import gate_migrations

_DB_DIR = Path(__file__).resolve().parents[2] / "src/episteme/data/db"
_MIGRATION_0015 = _DB_DIR / "migrations/0015_api_layer.sql"
_MIGRATION_0016 = _DB_DIR / "migrations/0016_api_perf.sql"
_MIGRATION = _DB_DIR / "migrations/0017_api_no_timeouts.sql"
_NEW_FAMILIES = {
    "literature_topic_scan",
    "literature_topic_chunk",
    "time_budget_ms",
    "search_work_mem_mb",
}


# ---------------------------------------------------------------------------
# non-pg
# ---------------------------------------------------------------------------
def test_0017_seeded_limits_equal_config():
    text = _MIGRATION.read_text(encoding="utf-8")
    block = re.search(
        r"INSERT INTO episteme_api_internal\.result_limit.*?VALUES(.*?)ON CONFLICT", text, re.S
    )
    assert block, "0017 must seed result_limit"
    seeded = {
        m.group(1): (int(m.group(2)), int(m.group(3)))
        for m in re.finditer(r"\('(\w+)',\s*(\d+),\s*(\d+)\)", block.group(1))
    }
    assert set(seeded) == _NEW_FAMILIES
    for family, values in seeded.items():
        assert values == config.API_RESULT_LIMITS[family], family


def test_0017_lowers_search_rank_chars_only_where_still_seeded():
    text = _MIGRATION.read_text(encoding="utf-8")
    update = re.search(
        r"UPDATE episteme_api_internal\.result_limit\s+SET default_limit = (\d+), "
        r"max_limit = (\d+)\s+WHERE family = 'search_rank_chars'"
        r"\s+AND default_limit = 10000 AND max_limit = 10000;",
        text,
    )
    assert update, "0017 must update search_rank_chars where it holds (10000, 10000)"
    values = (int(update.group(1)), int(update.group(2)))
    assert values == (2000, 2000)
    assert values == config.API_RESULT_LIMITS["search_rank_chars"]


def test_0017_is_one_transaction_and_creates_the_table():
    text = _MIGRATION.read_text(encoding="utf-8")
    assert text.count("BEGIN;") == 1 and text.rstrip().endswith("COMMIT;")
    assert "CREATE TABLE IF NOT EXISTS episteme.mesh_qualifier" in text
    assert "CREATE INDEX IF NOT EXISTS mesh_qualifier_lower_name_idx" in text
    assert "(lower(qualifier_name))" in text


def test_0017_gate_marker():
    assert gate_migrations.MIGRATION_MARKERS["0017_api_no_timeouts.sql"] == [
        "table:episteme.mesh_qualifier"
    ]


def test_grants_read_list_has_mesh_qualifier():
    text = (_DB_DIR / "grants/0015_api_tables.sql").read_text(encoding="utf-8")
    assert "'mesh_qualifier'" in text


# ---------------------------------------------------------------------------
# pg
# ---------------------------------------------------------------------------
@pytest.mark.pg
def test_0017_applies_after_0016_and_again(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(_MIGRATION_0015.read_text(encoding="utf-8"))
        cur.execute(_MIGRATION_0016.read_text(encoding="utf-8"))
        cur.execute(_MIGRATION.read_text(encoding="utf-8"))
        cur.execute(_MIGRATION.read_text(encoding="utf-8"))
        cur.execute("SELECT to_regclass('episteme.mesh_qualifier') IS NOT NULL")
        assert cur.fetchone()[0] is True
        cur.execute(
            "SELECT family, default_limit, max_limit FROM episteme_api_internal.result_limit "
            "WHERE family = ANY (%s)",
            (sorted(_NEW_FAMILIES | {"search_rank_chars"}),),
        )
        rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    pg_conn.commit()
    for family in _NEW_FAMILIES:
        assert rows[family] == config.API_RESULT_LIMITS[family], family
