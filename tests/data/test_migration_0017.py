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
_FUNCTION = re.compile(
    r"CREATE OR REPLACE FUNCTION (episteme_api(?:_internal)?\.\w+)\((.*?)\$\$(.*?)\$\$;", re.S
)


def _functions(path: Path) -> dict[str, str]:
    return {m.group(1): m.group(0) for m in _FUNCTION.finditer(path.read_text(encoding="utf-8"))}


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


def test_schema_sql_carries_the_0017_bodies():
    new = _functions(_MIGRATION)
    assert set(new) >= {
        "episteme_api_internal.literature_by_mesh",
        "episteme_api_internal.literature_topic_keep",
        "episteme_api_internal.search_text",
    }
    schema_text = (_DB_DIR / "schema.sql").read_text(encoding="utf-8")
    schema = _functions(_DB_DIR / "schema.sql")
    for name, text in new.items():
        assert schema.get(name) == text, name
        # The latest body only: no older copy of the function stays in schema.sql.
        assert schema_text.count(f"CREATE OR REPLACE FUNCTION {name}(") == 1, name


def test_0017_functions_set_search_path_and_definer():
    for name, text in _functions(_MIGRATION).items():
        header = text.split("$$", 1)[0]
        assert "SET search_path = pg_catalog, pg_temp" in header, name
        if re.search(r"\bepisteme\.[a-z]", text):
            assert "SECURITY DEFINER" in header, name


def test_literature_by_mesh_0017_note_texts_and_deadline():
    body = _functions(_MIGRATION)["episteme_api_internal.literature_by_mesh"]
    assert "'topic %s is not a MeSH descriptor or qualifier name'" in body
    assert "'partial: time budget of %s ms spent; %s'" in body
    assert "'years from %s down not searched'" in body
    assert "'records with an unknown year not searched'" in body
    assert "statement_timestamp() + make_interval(secs => v_budget / 1000.0)" in body
    assert "limit_for('time_budget_ms')" in body


def test_search_text_0017_body():
    body = _functions(_MIGRATION)["episteme_api_internal.search_text"]
    assert "limit_for('search_work_mem_mb')" in body
    assert "set_config('work_mem', v_old_wm, true)" in body
    # Short rank input for the labels only; other sources pass the raw columns (no detoast).
    assert "CASE WHEN v_src IN ('dailymed', 'ema_epar')" in body
    assert "pg_catalog.left(b.body_text, v_rank_chars)" in body
    assert "b.abstract, b.body_text, b.text_override)" in body
    assert "'partial: time budget of %s ms spent; not searched: %s'" in body
    assert "statement_timestamp()" in body and "limit_for('time_budget_ms')" in body
    # Every RETURN after the work_mem change comes after the restore.
    changed = body.index("set_config('work_mem',\n")
    restore = body.index("set_config('work_mem', v_old_wm, true)")
    assert "RETURN;" not in body[changed:restore]


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
