"""Migration 0018 head (mesh_term, mesh_term_vec, term_norm, families): seeds equal config,
one transaction, gate marker, grants, schema.sql parity (non-pg); idempotent apply after
0017 and SQL/Python term_norm parity (pg: episteme_test only)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from episteme import config
from episteme.data.db import fts_index
from episteme.ops import gate_migrations
from episteme.reference.term_norm import term_norm

_DB_DIR = Path(__file__).resolve().parents[2] / "src/episteme/data/db"
_MIGRATION_0015 = _DB_DIR / "migrations/0015_api_layer.sql"
_MIGRATION_0016 = _DB_DIR / "migrations/0016_api_perf.sql"
_MIGRATION_0017 = _DB_DIR / "migrations/0017_api_no_timeouts.sql"
_MIGRATION = _DB_DIR / "migrations/0018_question_terms.sql"
_NEW_FAMILIES = {"match_ngram_max", "suggest", "suggest_ef_search"}
_FUNCTION = re.compile(
    r"CREATE OR REPLACE FUNCTION (episteme_api(?:_internal)?\.\w+)\((.*?)\$\$(.*?)\$\$;", re.S
)


def _functions(path: Path) -> dict[str, str]:
    return {m.group(1): m.group(0) for m in _FUNCTION.finditer(path.read_text(encoding="utf-8"))}


# ---------------------------------------------------------------------------
# non-pg
# ---------------------------------------------------------------------------
def test_0018_seeded_limits_equal_config():
    text = _MIGRATION.read_text(encoding="utf-8")
    block = re.search(
        r"INSERT INTO episteme_api_internal\.result_limit.*?VALUES(.*?)ON CONFLICT", text, re.S
    )
    assert block, "0018 must seed result_limit"
    seeded = {
        m.group(1): (int(m.group(2)), int(m.group(3)))
        for m in re.finditer(r"\('(\w+)',\s*(\d+),\s*(\d+)\)", block.group(1))
    }
    assert set(seeded) == _NEW_FAMILIES
    for family, values in seeded.items():
        assert values == config.API_RESULT_LIMITS[family], family


def test_0018_is_one_transaction_with_tables_and_vector_guard():
    text = _MIGRATION.read_text(encoding="utf-8")
    assert text.count("BEGIN;") == 1 and text.rstrip().endswith("COMMIT;")
    assert "0018 needs the vector extension (scripts/data/db/install_extensions.sh)" in text
    assert "CREATE TABLE IF NOT EXISTS episteme.mesh_term (" in text
    assert "CREATE TABLE IF NOT EXISTS episteme.mesh_term_vec (" in text
    assert re.search(r"embedding\s+halfvec\(768\) NOT NULL", text)
    assert "mesh_term_vec_hnsw_idx" in text and "USING hnsw (embedding halfvec_ip_ops)" in text
    assert "ON episteme.mesh_term (term_norm)" in text
    assert text.count("ON episteme.mesh_term (descriptor_ui)") == 1
    assert "ON episteme.mesh_term_vec (descriptor_ui)" in text


def test_0018_gate_marker():
    assert gate_migrations.MIGRATION_MARKERS["0018_question_terms.sql"] == [
        "table:episteme.mesh_term",
        "table:episteme.mesh_term_vec",
    ]


# Functions that take a pgvector type: schema.sql runs without pgvector, so they live in
# 0018 only (as the mesh_term_vec table does).
_VECTOR_ONLY = {"episteme_api_internal.suggest_terms", "episteme_api.suggest_terms"}


def test_schema_sql_carries_the_0018_function_bodies():
    new = _functions(_MIGRATION)
    assert {
        "episteme_api_internal.term_norm",
        "episteme_api_internal.cap_terms",
        "episteme_api_internal.match_terms",
        "episteme_api.match_terms",
        "episteme_api_internal.search_terms",
        "episteme_api.search_terms",
    } <= set(new)
    assert _VECTOR_ONLY <= set(new)
    schema_text = (_DB_DIR / "schema.sql").read_text(encoding="utf-8")
    schema = _functions(_DB_DIR / "schema.sql")
    for name, text in new.items():
        if name in _VECTOR_ONLY:
            assert name not in schema, name
            continue
        assert schema.get(name) == text, name
        assert schema_text.count(f"CREATE OR REPLACE FUNCTION {name}(") == 1, name
    assert "episteme_api_internal.term_row AS (" in schema_text
    assert "halfvec" not in schema_text


def test_0018_api_functions_follow_the_0015_rules():
    """Every function that reads a table is SECURITY DEFINER and STABLE; every one sets
    search_path = pg_catalog, pg_temp; pgvector names are qualified with public."""
    for name, text in _functions(_MIGRATION).items():
        header = text.split("$$", 1)[0]
        body = text.split("$$", 1)[1]
        assert "SET search_path = pg_catalog, pg_temp" in header, name
        if re.search(r"\bepisteme\.[a-z]", body):
            assert "SECURITY DEFINER" in header and "STABLE" in header, name
        assert not re.search(r"(?<!public\.)halfvec", text), name
        assert "<#>" not in body or "OPERATOR(public.<#>)" in body, name


@pytest.mark.parametrize("path", [_MIGRATION, _DB_DIR / "schema.sql"], ids=["0018", "schema"])
def test_search_terms_uses_the_index_expression_and_the_fts_sources(path):
    """search_terms matches with the index expression (b. alias), so the planner can use
    the index of each partition, and searches the sources of the full-text allowlist."""
    body = _functions(path)["episteme_api_internal.search_terms"]
    expr = fts_index.fts_expression("b.")
    assert body.count(expr) == 2  # the match and the rank
    assert f"{expr} @@ v_q" in body
    assert 'ORDER BY b.article_id COLLATE "C"' in body and "LIMIT v_scan" in body
    m = re.search(r"c_covered\s+constant\s+text\[\]\s*:=\s*ARRAY\[([^\]]*)\]", body)
    assert m, "search_terms must declare c_covered"
    covered = tuple(s.strip().strip("'") for s in m.group(1).split(","))
    assert covered == fts_index.FTS_SOURCES
    assert "SET enable_seqscan = off" in body.split("$$", 1)[0]


def test_term_norm_function_is_a_pure_immutable_helper():
    text = _functions(_MIGRATION)["episteme_api_internal.term_norm"]
    header = text.split("$$", 1)[0]
    assert "IMMUTABLE" in header and "SECURITY DEFINER" not in header
    assert "SET search_path = pg_catalog, pg_temp" in header
    assert "[^[:alnum:]]+" in text


def test_grants_read_list_has_the_mesh_term_tables():
    text = (_DB_DIR / "grants/0015_api_tables.sql").read_text(encoding="utf-8")
    assert "'mesh_term'" in text and "'mesh_term_vec'" in text


# ---------------------------------------------------------------------------
# pg
# ---------------------------------------------------------------------------
_SAMPLES = [
    "Stevens-Johnson  Syndrome!",
    "Hb A1c",
    "Ångström",
    "",
    "  leading and trailing  ",
    "tab\tseparated\tterms",
    "TNF-α receptor",
    "β-lactam",
    "Ω-3 fatty acids",
    "COVID-19",
    "type 2 diabetes mellitus (T2DM)",
    "5-HT3 antagonist",
    "a_b_c",
    "O'Brien's syndrome",
    "naïve T cells",
    "Müller-Lyer; illusion",
    "ÉCOLE",
    r"x/y\z",
    "line\nbreak",
    "---",
]


@pytest.mark.pg
def test_term_norm_sql_equals_python(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(_MIGRATION.read_text(encoding="utf-8"))
        for text in _SAMPLES:
            cur.execute("SELECT episteme_api_internal.term_norm(%s)", (text,))
            assert cur.fetchone()[0] == term_norm(text), repr(text)
    pg_conn.commit()


@pytest.mark.pg
def test_0018_idempotent(pg_conn):
    with pg_conn.cursor() as cur:
        for path in (_MIGRATION_0015, _MIGRATION_0016, _MIGRATION_0017):
            cur.execute(path.read_text(encoding="utf-8"))
        cur.execute(_MIGRATION.read_text(encoding="utf-8"))
        cur.execute(_MIGRATION.read_text(encoding="utf-8"))
        cur.execute(
            "SELECT to_regclass('episteme.mesh_term') IS NOT NULL, "
            "to_regclass('episteme.mesh_term_vec') IS NOT NULL"
        )
        assert cur.fetchone() == (True, True)
        cur.execute(
            "SELECT family, default_limit, max_limit FROM episteme_api_internal.result_limit "
            "WHERE family = ANY (%s)",
            (sorted(_NEW_FAMILIES),),
        )
        rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    pg_conn.commit()
    for family in _NEW_FAMILIES:
        assert rows[family] == config.API_RESULT_LIMITS[family], family
