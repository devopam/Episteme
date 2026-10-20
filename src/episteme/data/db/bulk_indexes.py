"""Bulk-load mode: drop a named group of secondary indexes before a large load
and rebuild them after. Group ``articles`` (default) is the six secondary
indexes of ``episteme.articles``; ``pubmed_graph`` is the 14 secondary indexes
of the PubMed graph tables (migration 0006).

    python -m episteme.data.db.bulk_indexes drop|rebuild|status [--group G]

Operators run ``scripts/data/db/bulk_load_indexes.sh``; ``run_pipeline.sh
<source> load --bulk`` (group ``articles``) and ``<source> graph --bulk``
(group ``pubmed_graph``) call it around the stage. ``main()`` connects as
``episteme_sys_admin``, the table owner (``episteme_app`` cannot drop or create
indexes), through ``admin_dsn_from_settings()``, so the DB-mode guard applies,
and with ``pool_kwargs`` so ``read-only`` mode refuses the writes.

``drop`` saves each index of the group's ``pg_get_indexdef()`` in
``episteme._bulk_index_state`` (migration 0005) and drops it, in one
transaction with its audit row. ``rebuild`` re-creates each saved index in its
own transaction, deletes its row and writes its ``bulk_index_rebuild`` audit
row there (one per index), then runs ``ANALYZE`` on each table touched (not
audited). ``rebuild`` takes no group: it rebuilds every pending row. A row left in
the table means a rebuild is pending; ``drop`` refuses until it is done.

Exit codes: drop 0 ok / 2 rebuild pending / 1 error; rebuild 0 / 1; status 0
nothing pending / 1 pending / 3 error (e.g. migration 0005 not applied).
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sys
from typing import Any

import psycopg
from psycopg import sql

from episteme import audit_trail
from episteme.config import ConfigError, get_settings, require_actor
from episteme.data.db import connection as db_connection
from episteme.data.db.guard import ProductionDatabaseGuardError, pool_kwargs
from episteme.ops.status import current
from episteme.ops.stop import stage_guard

SCHEMA = "episteme"
# Parent indexes on the partitioned episteme.articles; dropping a parent index
# drops every partition's index. Kept on purpose: the primary keys,
# articles_source_file_idx and articles_id_idx (the loader's deletes use them)
# and articles_retrieved_at_brin (cheap to maintain).
COVERED_INDEXES: tuple[str, ...] = (
    "articles_pmid_idx",
    "articles_pmcid_idx",
    "articles_doi_idx",
    "articles_mesh_gin",
    "articles_authors_gin",
    "articles_publication_types_gin",
)
# (table, index) pairs per group. The permanent <table>_source_file_idx indexes
# (the loaders' retire/delete paths use them) are never in a group.
INDEX_GROUPS: dict[str, tuple[tuple[str, str], ...]] = {
    "articles": tuple(("articles", n) for n in COVERED_INDEXES),
    "pubmed_graph": (
        ("article_mesh", "article_mesh_pmid_idx"),
        ("article_mesh", "article_mesh_descriptor_ui_idx"),
        ("article_cites", "article_cites_src_pmid_idx"),
        ("article_cites", "article_cites_dst_pmid_idx"),
        ("article_comments", "article_comments_pmid_idx"),
        ("article_comments", "article_comments_ref_pmid_idx"),
        ("article_chemicals", "article_chemicals_pmid_idx"),
        ("article_chemicals", "article_chemicals_substance_ui_idx"),
        ("article_keywords", "article_keywords_pmid_idx"),
        ("article_keywords", "article_keywords_keyword_idx"),
        ("article_authors", "article_authors_pmid_idx"),
        ("article_authors", "article_authors_orcid_idx"),
        ("article_facets", "article_facets_pmid_idx"),
        ("article_facets", "article_facets_facet_descriptor_idx"),
    ),
}
# pg_get_indexdef() of a partitioned parent index reads "ON ONLY"; run verbatim
# it would create an empty, invalid parent index with no partition indexes.
_PARENT_DEF = re.compile(r"^CREATE INDEX (\S+) ON ONLY episteme\.(\w+) USING ")


class BulkIndexError(RuntimeError):
    """A covered index is missing, or a saved definition cannot be rebuilt."""


class PendingRebuildError(BulkIndexError):
    """A previous bulk load's rebuild never finished."""


def rebuild_sql(definition: str, name: str) -> str:
    """The saved parent definition with its one ``ON ONLY`` turned into ``ON``,
    which builds the index on every partition and attaches them. ``name`` is the
    index the definition must create."""
    m = _PARENT_DEF.match(definition)
    if not m:
        raise BulkIndexError(
            f"saved definition is not a parent index on an episteme table: {definition!r}"
        )
    if ";" in definition:
        raise BulkIndexError(f"saved definition for {name} contains ';': {definition!r}")
    if m.group(1) != name:
        raise BulkIndexError(f"saved definition creates {m.group(1)!r}, expected {name!r}")
    table = m.group(2)
    return definition.replace(
        f" ON ONLY episteme.{table} USING ", f" ON episteme.{table} USING ", 1
    )


def status(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT index_name, definition, run_id, dropped_at "
            "FROM episteme._bulk_index_state ORDER BY index_name"
        )
        rows = cur.fetchall()
    return [
        {"index_name": r[0], "definition": r[1], "run_id": r[2], "dropped_at": r[3]} for r in rows
    ]


def _index_validity(cur: psycopg.Cursor, name: str) -> bool | None:
    """True / False: an index of that name exists and is (in)valid; None: absent."""
    cur.execute(
        "SELECT i.indisvalid FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s",
        (SCHEMA, name),
    )
    row = cur.fetchone()
    return None if row is None else bool(row[0])


def _drop_index(cur: psycopg.Cursor, schema: str, name: str) -> None:
    with current().long_statement(f"DROP INDEX {name}"):
        cur.execute(
            sql.SQL("DROP INDEX {}.{}").format(sql.Identifier(schema), sql.Identifier(name))
        )


def drop(conn: psycopg.Connection, *, run_id: str | None, group: str = "articles") -> list[str]:
    """Save and drop every index of ``group``, in one transaction; commits."""
    require_actor()  # fail before any work, not at the audit row
    if group not in INDEX_GROUPS:
        raise BulkIndexError(f"unknown index group {group!r}; known: {sorted(INDEX_GROUPS)}")
    pairs = INDEX_GROUPS[group]
    names = [n for _, n in pairs]
    tables = sorted({t for t, _ in pairs})
    try:
        if status(conn):
            raise PendingRebuildError(
                "a previous bulk load's index rebuild never finished; "
                "run `bash scripts/data/db/bulk_load_indexes.sh rebuild` first"
            )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.relname, pg_get_indexdef(c.oid) FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = ANY(%s) AND i.indrelid = ANY(%s::regclass[])",
                (names, [f"{SCHEMA}.{t}" for t in tables]),
            )
            found = dict(cur.fetchall())
            missing = [n for n in names if n not in found]
            if missing:
                raise BulkIndexError(f"covered index(es) of group {group!r} not found: {missing}")
            for name in names:
                rebuild_sql(found[name], name)  # never drop what could not be rebuilt
            for name in names:
                cur.execute(
                    "INSERT INTO episteme._bulk_index_state (index_name, definition, run_id) "
                    "VALUES (%s, %s, %s)",
                    (f"{SCHEMA}.{name}", found[name], run_id),
                )
                _drop_index(cur, SCHEMA, name)
        audit_trail.record(
            "bulk_index_drop",
            conn=conn,
            object=f"{group} " + ",".join(names),
            rows_affected=len(names),
            run_id=run_id,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return names


def rebuild(conn: psycopg.Connection, *, run_id: str | None) -> list[str]:
    """Re-create every saved index, each in its own transaction; idempotent.

    Every saved definition is validated before anything is built. An index of
    the same name that already exists and is valid is kept; an invalid one is
    dropped and rebuilt. Returns the schema-qualified names handled."""
    require_actor()  # fail before hours of index work, not at the first audit row
    pending = status(conn)
    conn.commit()
    if not pending:
        return []
    statements = {
        row["index_name"]: rebuild_sql(row["definition"], row["index_name"].split(".", 1)[1])
        for row in pending
    }
    saved = {row["index_name"]: row["definition"] for row in pending}
    table_of = {k: _PARENT_DEF.match(v).group(2) for k, v in saved.items()}  # validated above
    tables = sorted(set(table_of.values()))

    done: list[str] = []
    for full_name, statement in statements.items():
        schema, name = full_name.split(".", 1)
        try:
            with conn.cursor() as cur:
                validity = _index_validity(cur, name)
                if validity is True:
                    cur.execute("SELECT pg_get_indexdef(%s::regclass)", (f"{schema}.{name}",))
                    (existing,) = cur.fetchone()
                    if existing != saved[full_name]:
                        raise BulkIndexError(
                            f"index {full_name} already exists and differs from the saved "
                            f"definition: {existing!r}"
                        )
                elif validity is False:
                    _drop_index(cur, schema, name)
                if validity is not True:
                    with current().long_statement(f"CREATE INDEX {name}"):
                        cur.execute(statement)
                cur.execute(
                    "DELETE FROM episteme._bulk_index_state WHERE index_name = %s", (full_name,)
                )
            audit_trail.record(
                "bulk_index_rebuild",
                conn=conn,
                object=f"{SCHEMA}.{table_of[full_name]} {name}",
                rows_affected=1,
                run_id=run_id,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        done.append(full_name)

    try:
        with conn.cursor() as cur:
            for table in tables:
                cur.execute(
                    sql.SQL("ANALYZE {}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(table))
                )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return done


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m episteme.data.db.bulk_indexes",
        description=("Drop or rebuild a group of secondary indexes around a bulk load."),
    )
    p.add_argument("action", choices=("drop", "rebuild", "status"))
    p.add_argument(
        "--group",
        choices=sorted(INDEX_GROUPS),
        default="articles",
        help="index group to drop (rebuild and status cover every pending row)",
    )
    args = p.parse_args(argv)

    failure = 3 if args.action == "status" else 1
    try:
        settings = get_settings()
        dsn = db_connection.admin_dsn_from_settings()
    except (ProductionDatabaseGuardError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return failure

    # status only reads: it must not wait behind a running load
    guard = (
        contextlib.nullcontext()
        if args.action == "status"
        else stage_guard(lambda: None, lock_stage="load", db_lock=True)
    )
    try:
        with guard, psycopg.connect(dsn, autocommit=False, **pool_kwargs(settings)) as conn:
            if args.action == "status":
                rows = status(conn)
                for r in rows:
                    print(
                        f"pending {r['index_name']} run_id={r['run_id']} "
                        f"dropped_at={r['dropped_at']}"
                    )
                print(f"pending={len(rows)}")
                return 1 if rows else 0
            if args.action == "drop":
                try:
                    names = drop(conn, run_id=settings.run_id, group=args.group)
                except PendingRebuildError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
                print(f"dropped {len(names)}: {', '.join(names)}")
                return 0
            names = rebuild(conn, run_id=settings.run_id)
            print(
                f"rebuilt {len(names)}: {', '.join(names)}"
                if names
                else "rebuilt 0 (nothing pending)"
            )
            return 0
    except (BulkIndexError, ConfigError, psycopg.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return failure


if __name__ == "__main__":
    raise SystemExit(main())
