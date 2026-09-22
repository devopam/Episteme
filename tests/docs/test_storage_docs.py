"""Docs 07/08 vs the shipped storage code. Read-only; no DB, no network."""

from __future__ import annotations

import dataclasses
import re

from episteme.config import Settings

from ._repo import REPO, doc

_SCHEMA = REPO / "src" / "episteme" / "data" / "db" / "schema.sql"
_MIGRATIONS = REPO / "src" / "episteme" / "data" / "db" / "migrations"


# created by scripts/data/db/migrate_database.sh, not by the SQL files
_SCRIPT_TABLES = {"_migrations"}


def _sql_text() -> str:
    parts = [_SCHEMA.read_text(encoding="utf-8")]
    parts += [p.read_text(encoding="utf-8") for p in sorted(_MIGRATIONS.glob("*.sql"))]
    return "\n".join(parts)


def _tables_defined() -> set[str]:
    return set(re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?episteme\.(\w+)", _sql_text()))


def test_docs_08_marks_iceberg_superseded_and_names_postgres():
    t = doc("08-data-storage-principles.md")
    assert "ADR-0001" in t or "0001-hybrid-storage-architecture" in t
    assert "superseded" in t.lower()
    assert "Postgres" in t and "Parquet" in t


def test_docs_07_uses_postgres_property_tables():
    t = doc("07-knowledge-graph-lessons.md")
    for term in ("article_cites", "article_mesh", "SQL/PGQ"):
        assert term in t, term
    assert "defer the graph DB" not in t
    assert "guard" in t.lower() and "NOTICE" in t


def test_referenced_adrs_exist():
    for name in (
        "0001-hybrid-storage-architecture.md",
        "0002-data-model-storage-and-partitioning.md",
    ):
        assert (REPO / "docs" / "adr" / name).is_file()


def test_tables_named_in_docs_exist_in_sql():
    defined = _tables_defined()
    for name in ("07-knowledge-graph-lessons.md", "08-data-storage-principles.md"):
        for tbl in set(re.findall(r"`episteme\.(\w+)", doc(name))):
            # "license" / "subset" / "config" are `episteme.<name>` spans that are not tables
            # (column tag names and the python config module)
            assert tbl in defined | _SCRIPT_TABLES | {"license", "subset", "config"}, (name, tbl)


def test_columns_named_for_edge_tables_exist_in_schema():
    sql = _sql_text()
    for tbl, cols in {
        "article_cites": ("src_pmid", "dst_pmid", "source_file"),
        "article_mesh": ("descriptor_ui", "descriptor_name", "major_topic", "qualifiers"),
        "article_parts": ("container_id", "part_id"),
        "mesh_hierarchy": ("parent_descriptor_ui", "child_descriptor_ui"),
    }.items():
        block = re.search(
            rf"CREATE TABLE (?:IF NOT EXISTS )?episteme\.{tbl} \((.*?)\)\s*(?:PARTITION|;)",
            sql,
            re.S,
        )
        assert block, tbl
        for c in cols:
            assert c in block.group(1), (tbl, c)
            assert c in doc("07-knowledge-graph-lessons.md") + doc("08-data-storage-principles.md")


def test_config_roots_named_in_docs08_exist():
    t = doc("08-data-storage-principles.md")
    fields = {f.name for f in dataclasses.fields(Settings)}
    for setting in ("data_root", "raw_root", "processed_root", "corpus_root"):
        assert setting in fields and f"`{setting}`" in t
    for env in (
        "EPISTEME_DATA_ROOT",
        "EPISTEME_RAW_ROOT",
        "EPISTEME_PROCESSED_ROOT",
        "EPISTEME_CORPUS_ROOT",
    ):
        assert env in t
        assert env in (REPO / "src" / "episteme" / "config.py").read_text(encoding="utf-8")


def test_paths_named_in_docs_exist():
    for name in ("07-knowledge-graph-lessons.md", "08-data-storage-principles.md"):
        for p in set(re.findall(r"`((?:src|scripts|docs|tests)/[\w./-]+\.\w+)`", doc(name))):
            assert (REPO / p).exists(), (name, p)


def test_pgq_guard_described_matches_schema():
    sql = _SCHEMA.read_text(encoding="utf-8")
    assert "CREATE PROPERTY GRAPH episteme_graph" in sql
    assert "syntax_error OR feature_not_supported" in sql
    assert "SQL/PGQ unavailable" in sql


def test_src_does_not_use_pyiceberg():
    src = "\n".join(p.read_text(encoding="utf-8") for p in (REPO / "src").rglob("*.py"))
    assert "pyiceberg" not in src.lower()


def _partitions(parent: str) -> list[str]:
    """Relations declared `PARTITION OF episteme.<parent>` in schema + migrations."""
    return re.findall(rf"episteme\.(\w+)\s+PARTITION OF episteme\.{parent}\b", _sql_text())


def test_articles_partition_ladders_match_docs_08():
    """pmc (schema.sql) and bookshelf (migration 0002) have year ladders; nothing else does."""
    schema = _SCHEMA.read_text(encoding="utf-8")
    mig2 = next(_MIGRATIONS.glob("0002_*.sql")).read_text(encoding="utf-8")
    assert re.search(
        r"articles_pmc\s+PARTITION OF episteme\.articles\s+FOR VALUES IN \('pmc'\)"
        r"\s+PARTITION BY RANGE \(year\)",
        schema,
    )
    assert "PARTITION OF episteme.articles DEFAULT" in schema
    assert re.search(
        r"articles_bookshelf\s+PARTITION OF episteme\.articles\s+FOR VALUES IN "
        r"\('bookshelf'\)\s+PARTITION BY RANGE \(year\)",
        mig2,
    )
    assert len(re.findall(r"PARTITION BY RANGE \(year\)", _sql_text())) == 2
    assert len(_partitions("articles_pmc")) >= 3
    assert len(_partitions("articles_bookshelf")) >= 3
    t = doc("08-data-storage-principles.md")
    assert "`pmc` sub-partitioned `RANGE (year)`" in t
    assert "`bookshelf` also has a year ladder" in t


def test_article_body_is_list_source_only():
    assert set(_partitions("article_body")) == {
        "article_body_pmc",
        "article_body_default",
        "article_body_bookshelf",
    }
    assert re.search(
        r"CREATE TABLE episteme\.article_body \(.*?\) PARTITION BY LIST \(source\)",
        _sql_text(),
        re.S,
    )
    t = doc("08-data-storage-principles.md")
    assert "`LIST (source)` (`pmc`, `DEFAULT`; `bookshelf` via migration 0002)" in t


def test_hash_keys_and_bucket_counts_named_in_docs_08_match_schema():
    sql = _sql_text()
    t = doc("08-data-storage-principles.md")
    keys = {
        "article_cites": "src_pmid",
        "article_mesh": "pmid",
        "chunks": "article_id",
        "article_parts": "container_id",
    }
    for tbl, key in keys.items():
        assert re.search(
            rf"CREATE TABLE (?:IF NOT EXISTS )?episteme\.{tbl} \(.*?\) PARTITION BY HASH \({key}\)",
            sql,
            re.S,
        ), tbl
        assert f"| `episteme.{tbl}` | `HASH" in t, tbl
    for tbl in ("article_cites", "article_mesh", "chunks"):
        assert f"`HASH ({keys[tbl]})`, 8 buckets" in t, tbl
        n = len(
            re.findall(
                rf"episteme\.{tbl}_h\d PARTITION OF episteme\.{tbl} FOR VALUES WITH \(MODULUS 8",
                sql,
            )
        )
        assert n == 8, tbl
    # article_parts: 8 buckets, created in a loop over 0..7
    assert "FOR i IN 0..7 LOOP" in sql and "MODULUS 8, REMAINDER %s" in sql


def test_om_settings_are_consumed_nowhere_outside_config():
    """docs/08: OM_HOST / OM_JWT are read into Settings but nothing consumes them."""
    paths = list((REPO / "src").rglob("*.py")) + list((REPO / "scripts").rglob("*.sh"))
    for p in paths:
        if p.name == "config.py" and p.parent.name == "episteme":
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for token in ("OM_HOST", "OM_JWT", "om_host", "om_jwt"):
            assert token not in text, (p, token)


def test_graph_builder_derives_cites_and_mesh_for_pmc_only():
    path = REPO / "src" / "episteme" / "data" / "graph_builder.py"
    src = path.read_text(encoding="utf-8")
    build = src.split("\ndef build", 1)[1]
    before_pmc, rest = build.split('if source == "pmc":', 1)
    pmc_block = rest.split('if source == "mesh":', 1)[0]
    assert "_CITES_INSERT" in pmc_block and "_MESH_INSERT" in pmc_block
    assert "_CITES_INSERT" not in before_pmc and "_MESH_INSERT" not in before_pmc
    assert "_CITES_INSERT" not in rest.split('if source == "mesh":', 1)[1]
