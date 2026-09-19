"""Idempotent per-``source_file`` loader: staging shard -> Postgres.

``load_source_file`` runs as ONE transaction (it never commits -- the caller
owns the transaction boundary):

    1. read the staging shard (Polars) into a list of row dicts
    2. DELETE FROM episteme.article_body for every article of the replaced
       source_file(s) (via the articles.id linkage, plus the shard's own ids)
    3. DELETE FROM episteme.articles      WHERE source_file = ANY(<file>)   (per file)
    4. COPY the hot (non-text) columns into episteme.articles
    5. COPY (article_id, source, year + 4 text cols) into episteme.article_body
    6. INSERT one episteme._lineage row per distinct source_file
    7. audit_trail.record("load_replace" if anything was deleted else "load_commit")

Idempotency is DELETE + COPY keyed on ``source_file`` (never ``ON CONFLICT`` --
``episteme.articles`` has no primary key). ``article_body`` has no
``source_file`` column, so its idempotent delete is keyed on the shard's ``id``
values instead, and must run *before* the ``articles`` delete.

``year`` is the RANGE sub-partition key for ``pmc`` and ``NULL`` has no
partition, so every row's ``year`` is coerced ``None -> 0`` before COPY
(``year=0`` lands in ``articles_pmc_y0``).

This module never reads ``os.environ`` -- config comes from ``episteme.config``
via the helpers it calls (``audit_trail``).
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from episteme import audit_trail
from episteme.data import article_schema

# The "hot" narrow-table columns = ARTICLE_COLUMNS minus the four text columns
# that live in episteme.article_body. Derived (not hand-typed) so it cannot
# drift from the schema; the order matches schema.sql's episteme.articles DDL.
_TEXT_COLUMNS = ("title", "abstract", "body_text", "text")
HOT_COLUMNS: list[str] = [c for c in article_schema.ARTICLE_COLUMNS if c not in _TEXT_COLUMNS]

# episteme.article_body column order for the COPY. article_id holds the shard
# row's `id` value (the name differs on purpose).
BODY_COLUMNS: tuple[str, ...] = (
    "article_id",
    "source",
    "year",
    "title",
    "abstract",
    "body_text",
    "text",
)

# SP1-β loads pmc only. These sources are identifier enrichment, not a text
# source -- id_mappings would route to episteme.id_map. Documented, not exercised.
_ID_MAP_SOURCES = ("europepmc_lite", "europepmc_id_mappings")


def _read_shard(staging_path: Path) -> list[dict[str, Any]]:
    """Read a Parquet or JSONL staging shard into a list of row dicts (no pandas)."""
    import polars as pl

    name = staging_path.name.lower()
    if name.endswith(".parquet"):
        frame = pl.read_parquet(staging_path)
    elif name.endswith((".jsonl", ".ndjson")):
        frame = pl.read_ndjson(staging_path)
    else:
        raise ValueError(f"unsupported staging shard format: {staging_path}")
    return frame.to_dicts()


def _coerce_year(value: Any) -> int:
    """``year`` partition sentinel: NULL / unparseable -> 0 (articles_pmc_y0)."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _coerce_retrieved_at(value: Any) -> Any:
    """The shard stores retrieved_at as an ISO string; hand psycopg a datetime.

    Tolerates None / empty (-> None) and falls back to the raw value if it does
    not parse (psycopg / Postgres then casts the text literal).
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return value


def _hot_value(row: dict[str, Any], col: str) -> Any:
    if col == "year":
        return _coerce_year(row.get("year"))
    if col == "retrieved_at":
        return _coerce_retrieved_at(row.get("retrieved_at"))
    return row.get(col)


def load_source_file(
    conn,
    *,
    source: str,
    staging_path: Path | str,
    run_id: str,
) -> dict[str, Any]:
    """Load one staging shard into episteme.articles + episteme.article_body.

    One transaction; the caller commits. Returns a stats dict:
    ``{source_file, rows, deleted, body_deleted, event, staging_path}``.
    """
    staging_path = Path(staging_path)

    if source in _ID_MAP_SOURCES:
        # SP1-β: pmc only; id_mappings would route to episteme.id_map here.
        raise NotImplementedError(f"{source} loading is SP2+")

    rows = _read_shard(staging_path)
    content_hash = hashlib.sha256(staging_path.read_bytes()).hexdigest()

    deleted_ids = [r["id"] for r in rows]
    source_files = sorted({r["source_file"] for r in rows})

    deleted = 0
    per_file_deleted: dict[str, int] = {}

    with conn.cursor() as cur:
        # (c) article_body first. article_body has no source_file column, so
        # target it by the linkage articles.id == article_body.article_id for
        # the source_file(s) being replaced -- this also removes rows whose id
        # is dropped by a shrinking re-load (which the articles delete below
        # would otherwise orphan) -- OR-ed with the incoming shard's ids for
        # the belt-and-braces case of a pre-existing body row for a new id.
        # scoped by source: source_file basenames are not globally unique across sources
        cur.execute(
            """
            DELETE FROM episteme.article_body
             WHERE article_id IN (
                     SELECT id FROM episteme.articles WHERE source_file = ANY(%s) AND source = %s
                   )
                OR (article_id = ANY(%s) AND source = %s)
            """,
            (source_files, source, deleted_ids, source),
        )
        body_deleted = cur.rowcount

        # (d0) id-collision guard: a periodic full-dump re-release (SP4's
        # normal re-run shape) may reuse a native id under a DIFFERENT
        # source_file than the one that first wrote it. Delete any such row
        # here. The NOT(...) predicate keeps this delete disjoint from the
        # per-source_file loop below (that loop's predicate is
        # source_file = ANY([sf]) -- the exact complement of this one) --
        # neither delete can ever match a row the other matches, so
        # per_file_deleted[sf]'s accounting in the loop below stays exact
        # with no double-counting. Scoped by source, same as every other
        # delete here (source_file basenames are not globally unique across
        # sources).
        cur.execute(
            "DELETE FROM episteme.articles "
            "WHERE id = ANY(%s) AND source = %s AND NOT (source_file = ANY(%s))",
            (deleted_ids, source, source_files),
        )
        deleted += cur.rowcount

        # (d) articles, per source_file, so _lineage gets per-file counts.
        # scoped by source: source_file basenames are not globally unique across sources
        for sf in source_files:
            cur.execute(
                "DELETE FROM episteme.articles WHERE source_file = ANY(%s) AND source = %s",
                ([sf], source),
            )
            per_file_deleted[sf] = cur.rowcount
            deleted += cur.rowcount

        # (e) COPY the hot columns into episteme.articles.
        hot_cols_sql = ", ".join(HOT_COLUMNS)
        with cur.copy(f"COPY episteme.articles ({hot_cols_sql}) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(tuple(_hot_value(r, c) for c in HOT_COLUMNS))

        # (f) COPY the text columns into episteme.article_body.
        body_cols_sql = ", ".join(BODY_COLUMNS)
        with cur.copy(f"COPY episteme.article_body ({body_cols_sql}) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(
                    (
                        r["id"],
                        r.get("source"),
                        _coerce_year(r.get("year")),
                        r.get("title"),
                        r.get("abstract"),
                        r.get("body_text"),
                        r.get("text"),
                    )
                )

        # (g) one _lineage row per distinct source_file (pmc shards have one).
        for sf in source_files:
            rows_inserted = sum(1 for r in rows if r["source_file"] == sf)
            cur.execute(
                """
                INSERT INTO episteme._lineage
                    (source, source_file, run_id, input_content_hash,
                     rows_inserted, rows_deleted)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (source, sf, run_id, content_hash, rows_inserted, per_file_deleted[sf]),
            )

    # (h) audit -- in conn's transaction, no commit.
    event = "load_replace" if deleted else "load_commit"
    audit_trail.record(
        event,
        conn=conn,
        object=f"{source} {staging_path.name}",
        input_content_hash=content_hash,
        rows_affected=len(rows),
        run_id=run_id,
    )

    return {
        "source_file": source_files,
        "rows": len(rows),
        "deleted": deleted,
        "body_deleted": body_deleted,
        "event": event,
        "staging_path": str(staging_path),
    }
