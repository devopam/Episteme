"""Migration 0015 skeleton and the settings sync (pg: episteme_test only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from episteme.config import LOOKUP_SOURCE_SUBSETS, api_result_limits
from episteme.reference import api_settings

pytestmark = pytest.mark.pg

_MIGRATION = (
    Path(__file__).resolve().parents[2] / "src/episteme/data/db/migrations/0015_api_layer.sql"
)
_CLAIM_COLUMNS = [
    "kind",
    "claim_group",
    "alt_rank",
    "text",
    "value",
    "method",
    "confidence",
    "confidence_basis",
    "source",
    "release_id",
    "source_file",
    "record_key",
    "jurisdiction",
    "licence",
]


def _apply(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(_MIGRATION.read_text(encoding="utf-8"))
    conn.commit()


def _one(conn, query, params=()):
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchone()[0]


def test_0015_is_idempotent(pg_conn):
    _apply(pg_conn)
    _apply(pg_conn)
    assert _one(pg_conn, "SELECT to_regtype('episteme_api_internal.claim_row')") is not None
    assert _one(pg_conn, "SELECT to_regtype('episteme_api_internal.article_row')") is not None
    assert _one(pg_conn, "SELECT to_regnamespace('episteme_api')") is not None


def test_claim_row_has_the_shared_columns(pg_conn):
    _apply(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT a.attname FROM pg_attribute a "
            "WHERE a.attrelid = 'episteme_api_internal.claim_row'::regclass "
            "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum"
        )
        assert [r[0] for r in cur.fetchall()] == _CLAIM_COLUMNS


def test_norm_name_matches_nfkc_lower(pg_conn):
    _apply(pg_conn)
    assert _one(pg_conn, "SELECT episteme_api_internal.norm_name(%s)", ("Ｃlopidogrel",)) == (
        "clopidogrel"
    )
    assert _one(pg_conn, "SELECT episteme_api_internal.norm_name(NULL)") is None


def test_licence_for_unlisted(pg_conn):
    _apply(pg_conn)
    api_settings.sync(pg_conn)
    assert _one(pg_conn, "SELECT episteme_api_internal.licence_for('no_such_source')") == "unlisted"
    assert _one(pg_conn, "SELECT episteme_api_internal.licence_for('substance')") == "commercial"


def test_limit_for_reads_table(pg_conn):
    _apply(pg_conn)
    api_settings.sync(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE episteme_api_internal.result_limit SET default_limit = 7 "
            "WHERE family = 'search'"
        )
    assert _one(pg_conn, "SELECT episteme_api_internal.limit_for('search')") == 7
    with pg_conn.cursor() as cur:
        with pytest.raises(Exception, match="no result limit"):
            cur.execute("SELECT episteme_api_internal.limit_for('nope')")
    pg_conn.rollback()


def test_sync_writes_config(pg_conn):
    _apply(pg_conn)
    counts = api_settings.sync(pg_conn)
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT source, subset FROM episteme_api_internal.source_subset")
        assert dict(cur.fetchall()) == LOOKUP_SOURCE_SUBSETS
        cur.execute(
            "SELECT family, default_limit, max_limit FROM episteme_api_internal.result_limit"
        )
        assert {f: (d, m) for f, d, m in cur.fetchall()} == api_result_limits()
    assert counts == {
        "source_subset": len(LOOKUP_SOURCE_SUBSETS),
        "result_limit": len(api_result_limits()),
    }


def test_0015_upgrades_the_first_draft_shapes(pg_conn):
    """A database with the first 0015 draft (claim_row.confidence real, substance_family
    without provenance columns) gets the final shapes when 0015 is applied again."""
    _apply(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("DROP FUNCTION IF EXISTS episteme_api.substance_family(bigint)")
        cur.execute("DROP FUNCTION IF EXISTS episteme_api_internal.substance_family(bigint)")
        cur.execute(
            "ALTER TYPE episteme_api_internal.claim_row ALTER ATTRIBUTE confidence TYPE real"
        )
        cur.execute(
            "CREATE FUNCTION episteme_api_internal.substance_family(p_substance_key bigint) "
            "RETURNS TABLE (substance_key bigint, moiety_key bigint, unii text, "
            "preferred_name text) LANGUAGE sql AS $$ SELECT 1::bigint, 1::bigint, "
            "NULL::text, NULL::text WHERE false $$"
        )
        cur.execute(
            "CREATE FUNCTION episteme_api.substance_family(p_substance_key bigint) "
            "RETURNS TABLE (substance_key bigint, moiety_key bigint, unii text, "
            "preferred_name text) LANGUAGE sql AS $$ SELECT * FROM "
            "episteme_api_internal.substance_family(p_substance_key) $$"
        )
    pg_conn.commit()
    _apply(pg_conn)
    assert (
        _one(
            pg_conn,
            "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
            "WHERE a.attrelid = 'episteme_api_internal.claim_row'::regclass "
            "AND a.attname = 'confidence'",
        )
        == "double precision"
    )
    for schema in ("episteme_api_internal", "episteme_api"):
        result = _one(
            pg_conn,
            "SELECT pg_get_function_result(%s::regprocedure)",
            (f"{schema}.substance_family(bigint)",),
        )
        assert result.endswith("source text, release_id text, licence text)")
