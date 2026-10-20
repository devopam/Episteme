"""Marker checks that prove a migration was applied (ruling R8).

``episteme._migrations`` is not reliable: migrations were applied by hand, so the
production table is empty and ``episteme_test`` has none. ``missing_markers``
therefore never reads ``_migrations``. It checks the objects each migration
creates, read-only (``to_regclass``, ``information_schema.columns``,
``pg_indexes``).

A marker is ``table:<schema>.<name>``, ``column:<schema>.<table>.<column>`` or
``index:<schema>.<name>``. Every migration file needs an entry (a test enforces
it). Note: the 0004 indexes are dropped on purpose during a bulk load, so 0004
can show as missing while a load is in progress.
"""

from __future__ import annotations

from typing import Any

MIGRATION_MARKERS: dict[str, list[str]] = {
    "0001_add_chunk_vector_columns.sql": [
        "column:episteme.chunks.embedding",
        "column:episteme.chunks.chunk_tsv",
    ],
    "0002_container_and_book_parts.sql": [
        "column:episteme.articles.container_id",
        "table:episteme.articles_bookshelf",
        "table:episteme.article_parts",
    ],
    "0003_mesh_hierarchy.sql": [
        "table:episteme.mesh_hierarchy",
        "index:episteme.mesh_hierarchy_uq",
    ],
    "0004_articles_load_indexes.sql": [
        "index:episteme.articles_source_file_idx",
        "index:episteme.articles_id_idx",
    ],
    "0005_bulk_index_state.sql": ["table:episteme._bulk_index_state"],
    "0006_pubmed_graph.sql": [
        "table:episteme.article_facets",
        "table:episteme._graph_lineage",
        "column:episteme.article_cites.dst_doi",
    ],
    "0007_pubmed_partition.sql": [
        "table:episteme.articles_pubmed",
        "table:episteme.article_body_pubmed",
    ],
    "0008_pmc_bookshelf_default_partition.sql": [
        "table:episteme.articles_pmc_default",
        "table:episteme.articles_bookshelf_default",
    ],
    "0009_apollo_pubchem_partitions.sql": [
        "table:episteme.articles_apollo",
        "table:episteme.articles_pubchem",
    ],
}


def _present(cur: Any, marker: str) -> bool:
    kind, _, ref = marker.partition(":")
    if kind == "table":
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (ref,))
    elif kind == "column":
        schema, table, column = ref.split(".")
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s AND column_name = %s)",
            (schema, table, column),
        )
    elif kind == "index":
        schema, name = ref.split(".")
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = %s AND indexname = %s)",
            (schema, name),
        )
    else:
        raise ValueError(f"unknown migration marker: {marker}")
    return bool(cur.fetchone()[0])


def missing_markers(conn: Any, names: list[str]) -> dict[str, list[str]]:
    """For each migration in ``names`` with a missing object: the missing markers.
    A name with no entry in ``MIGRATION_MARKERS`` reports ``["no markers defined"]``."""
    out: dict[str, list[str]] = {}
    with conn.cursor() as cur:
        for name in names:
            markers = MIGRATION_MARKERS.get(name)
            if markers is None:
                out[name] = ["no markers defined"]
                continue
            gone = [m for m in markers if not _present(cur, m)]
            if gone:
                out[name] = gone
    return out
