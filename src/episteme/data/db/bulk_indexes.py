"""Bulk-load mode: drop the secondary indexes of ``episteme.articles`` before a
large load and rebuild them after.

    python -m episteme.data.db.bulk_indexes drop|rebuild|status

Operators run ``scripts/data/db/bulk_load_indexes.sh``; ``run_pipeline.sh
<source> load --bulk`` calls it around the load stage. ``main()`` connects as
``episteme_sys_admin``, the table owner (``episteme_app`` cannot drop or create
indexes), through ``admin_dsn_from_settings()``, so the DB-mode guard applies,
and with ``pool_kwargs`` so ``read-only`` mode refuses the writes.

``drop`` saves each covered index's ``pg_get_indexdef()`` in
``episteme._bulk_index_state`` (migration 0005) and drops it, in one
transaction with its audit row. ``rebuild`` re-creates each saved index in its
own transaction, deletes its row and writes its ``bulk_index_rebuild`` audit
row there (one per index), then runs ``ANALYZE`` (not audited). A row left in
the table means a rebuild is pending; ``drop`` refuses until it is done.

Exit codes: drop 0 ok / 2 rebuild pending / 1 error; rebuild 0 / 1; status 0
nothing pending / 1 pending / 3 error (e.g. migration 0005 not applied).
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

import psycopg
from psycopg import sql

from episteme import audit_trail
from episteme.config import ConfigError, get_settings, require_actor
from episteme.data.db import connection as db_connection
from episteme.data.db.guard import ProductionDatabaseGuardError, pool_kwargs

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
# pg_get_indexdef() of a partitioned parent index reads "ON ONLY"; run verbatim
# it would create an empty, invalid parent index with no partition indexes.
_PARENT_DEF = re.compile(r"^CREATE INDEX (\S+) ON ONLY episteme\.articles USING ")


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
            f"saved definition is not a parent index on episteme.articles: {definition!r}"
        )
    if ";" in definition:
        raise BulkIndexError(f"saved definition for {name} contains ';': {definition!r}")
    if m.group(1) != name:
        raise BulkIndexError(f"saved definition creates {m.group(1)!r}, expected {name!r}")
    return definition.replace(
        " ON ONLY episteme.articles USING ", " ON episteme.articles USING ", 1
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
    cur.execute(sql.SQL("DROP INDEX {}.{}").format(sql.Identifier(schema), sql.Identifier(name)))


def drop(conn: psycopg.Connection, *, run_id: str | None) -> list[str]:
    """Save and drop every covered index, in one transaction; commits."""
    require_actor()  # fail before any work, not at the audit row
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
                "WHERE i.indrelid = 'episteme.articles'::regclass AND c.relname = ANY(%s)",
                (list(COVERED_INDEXES),),
            )
            found = dict(cur.fetchall())
            missing = [n for n in COVERED_INDEXES if n not in found]
            if missing:
                raise BulkIndexError(f"covered index(es) not found on episteme.articles: {missing}")
            for name in COVERED_INDEXES:
                rebuild_sql(found[name], name)  # never drop what could not be rebuilt
            for name in COVERED_INDEXES:
                cur.execute(
                    "INSERT INTO episteme._bulk_index_state (index_name, definition, run_id) "
                    "VALUES (%s, %s, %s)",
                    (f"{SCHEMA}.{name}", found[name], run_id),
                )
                _drop_index(cur, SCHEMA, name)
        audit_trail.record(
            "bulk_index_drop",
            conn=conn,
            object="episteme.articles " + ",".join(COVERED_INDEXES),
            rows_affected=len(COVERED_INDEXES),
            run_id=run_id,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return list(COVERED_INDEXES)


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
                    cur.execute(statement)
                cur.execute(
                    "DELETE FROM episteme._bulk_index_state WHERE index_name = %s", (full_name,)
                )
            audit_trail.record(
                "bulk_index_rebuild",
                conn=conn,
                object=f"episteme.articles {name}",
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
            cur.execute("ANALYZE episteme.articles")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return done


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m episteme.data.db.bulk_indexes",
        description=(
            "Drop or rebuild the secondary indexes of episteme.articles around a bulk load."
        ),
    )
    p.add_argument("action", choices=("drop", "rebuild", "status"))
    args = p.parse_args(argv)

    failure = 3 if args.action == "status" else 1
    try:
        settings = get_settings()
        dsn = db_connection.admin_dsn_from_settings()
    except (ProductionDatabaseGuardError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return failure

    try:
        with psycopg.connect(dsn, autocommit=False, **pool_kwargs(settings)) as conn:
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
                    names = drop(conn, run_id=settings.run_id)
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
