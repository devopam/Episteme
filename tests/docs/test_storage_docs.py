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
            assert tbl in defined | _SCRIPT_TABLES | {
                "license",
                "subset",
                "config",
            }  # tag names and the python module, (name, tbl)


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


def test_docs_do_not_claim_iceberg_is_in_use():
    src = "\n".join(p.read_text(encoding="utf-8") for p in (REPO / "src").rglob("*.py"))
    assert "pyiceberg" not in src.lower()
