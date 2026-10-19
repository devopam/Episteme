"""Idempotent per-``source_file`` loader: staging shard -> Postgres.

``load_source_file`` runs as ONE transaction (it never commits -- the caller
owns the transaction boundary):

    1. hash the shard in 1 MiB chunks; COPY its ids into a temp table
       ``pg_temp._load_ids`` (ON COMMIT DROP); collect its distinct source_file(s)
    2. DELETE FROM episteme.article_body for every article of the replaced
       source_file(s) (via the articles.id linkage), then for the shard's own ids
    3. DELETE FROM episteme.articles: an incoming id under another source_file,
       then WHERE source_file = ANY(<file>) per file
    4. stream the shard in batches; per batch COPY the hot (non-text) columns
       into episteme.articles,
    5. ... then (article_id, source, year + 4 text cols) into episteme.article_body
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
import itertools
import json
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from episteme import audit_trail
from episteme.data import article_schema
from episteme.ops.profile import default_batch_rows

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

# Rows per streamed batch (the loader's memory bound) and the content-hash
# read size.
LOAD_BATCH_ROWS = default_batch_rows()
_HASH_CHUNK_BYTES = 1 << 20

# SP1-β loads pmc only. These sources are identifier enrichment, not a text
# source -- id_mappings would route to episteme.id_map. Documented, not exercised.
_ID_MAP_SOURCES = ("europepmc_lite", "europepmc_id_mappings")


def _read_shard(staging_path: Path) -> list[dict[str, Any]]:
    """Read a Parquet or JSONL staging shard into a list of row dicts (no pandas).

    Whole-shard read: ``load_source_file`` no longer uses it (it streams);
    kept for compatibility only.
    """
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


def _file_sha256(path: Path) -> str:
    """SHA-256 hex of the whole file, read in 1 MiB chunks (same digest as read_bytes())."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_HASH_CHUNK_BYTES):
            h.update(chunk)
    return h.hexdigest()


def _shard_format(staging_path: Path) -> str:
    name = staging_path.name.lower()
    if name.endswith(".parquet"):
        return "parquet"
    if name.endswith((".jsonl", ".ndjson")):
        return "jsonl"
    raise ValueError(f"unsupported staging shard format: {staging_path}")


def _iter_jsonl(staging_path: Path) -> Iterator[dict[str, Any]]:
    with staging_path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def _iter_batches(staging_path: Path, fmt: str, batch_size: int) -> Iterator[dict[str, list[Any]]]:
    """Per batch of at most ``batch_size`` rows: every ``ARTICLE_COLUMNS`` name ->
    its values, one per row (never the whole shard). Parquet columns are
    converted once per column (``to_pylist``), not once per row as a dict. A
    column absent from the shard reads as all ``None``, as ``dict.get`` did.
    ``id`` and ``source_file`` are required (``KeyError`` if absent), as before."""
    columns = article_schema.ARTICLE_COLUMNS
    if fmt == "parquet":
        import pyarrow.parquet as pq

        with pq.ParquetFile(staging_path) as pf:
            names = set(pf.schema_arrow.names)
            for required in ("id", "source_file"):
                if required not in names:
                    raise KeyError(required)
            present = [c for c in columns if c in names]
            for rb in pf.iter_batches(batch_size=batch_size, columns=present):
                n = rb.num_rows
                yield {c: (rb.column(c).to_pylist() if c in names else [None] * n) for c in columns}
        return
    it = _iter_jsonl(staging_path)
    while batch := list(itertools.islice(it, batch_size)):
        cols = {c: [r.get(c) for r in batch] for c in columns}
        cols["id"] = [r["id"] for r in batch]
        cols["source_file"] = [r["source_file"] for r in batch]
        yield cols


def _batch_tuples(cols: dict[str, list[Any]]) -> tuple[list[tuple], list[tuple]]:
    """(episteme.articles tuples in HOT_COLUMNS order, episteme.article_body
    tuples in BODY_COLUMNS order) -- the same values the old per-row dict path
    produced (``_coerce_year``, ``_coerce_retrieved_at`` applied per value)."""
    years = [_coerce_year(v) for v in cols["year"]]
    hot_lists = []
    for c in HOT_COLUMNS:
        if c == "year":
            hot_lists.append(years)
        elif c == "retrieved_at":
            hot_lists.append([_coerce_retrieved_at(v) for v in cols[c]])
        elif c in article_schema.GIN_LIST_COLUMNS:
            # Shards staged before the cap (schema 1.5) can still hold oversized elements.
            hot_lists.append([article_schema.cap_list_elements(v) for v in cols[c]])
        else:
            hot_lists.append(cols[c])
    hot = list(zip(*hot_lists, strict=True))
    body = list(
        zip(
            cols["id"],
            cols["source"],
            years,
            cols["title"],
            cols["abstract"],
            cols["body_text"],
            cols["text"],
            strict=True,
        )
    )
    return hot, body


def _iter_id_batches(
    staging_path: Path, fmt: str, batch_size: int
) -> Iterator[tuple[list[Any], list[Any]]]:
    """(ids, source_files) in batches -- the ``id``/``source_file`` columns only."""
    if fmt == "parquet":
        import pyarrow.parquet as pq

        with pq.ParquetFile(staging_path) as pf:
            for rb in pf.iter_batches(batch_size=batch_size, columns=["id", "source_file"]):
                yield rb.column("id").to_pylist(), rb.column("source_file").to_pylist()
        return
    it = _iter_jsonl(staging_path)
    while batch := list(itertools.islice(it, batch_size)):
        yield [r["id"] for r in batch], [r["source_file"] for r in batch]


def load_source_file(
    conn,
    *,
    source: str,
    staging_path: Path | str,
    run_id: str,
    batch_size: int = LOAD_BATCH_ROWS,
) -> dict[str, Any]:
    """Load one staging shard into episteme.articles + episteme.article_body.

    One transaction; the caller commits. Rows stream through in batches of
    ``batch_size``, so peak memory is bounded by the batch, not the shard.
    Returns a stats dict:
    ``{source_file, rows, deleted, body_deleted, event, staging_path, read_sec,
    copy_sec, rows_per_sec}``. ``read_sec`` is time reading and tuple-building,
    ``copy_sec`` time in COPY; ``rows_per_sec`` is over the whole call (hash,
    deletes, COPY).
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    t_start = time.perf_counter()
    staging_path = Path(staging_path)

    if source in _ID_MAP_SOURCES:
        # SP1-β: pmc only; id_mappings would route to episteme.id_map here.
        raise NotImplementedError(f"{source} loading is SP2+")

    fmt = _shard_format(staging_path)
    content_hash = _file_sha256(staging_path)

    deleted = 0
    per_file_deleted: dict[str, int] = {}

    with conn.cursor() as cur:
        # The shard's ids go into a temp table, never a bound array: Postgres
        # compares `col = ANY($array)` linearly per row (no hashing), which made
        # a ~570k-id article_body delete run for 15+ minutes. Joins and
        # IN (SELECT ...) against an ANALYZEd table plan as hash/index lookups.
        # DROP first so two loads in one transaction do not collide.
        cur.execute("DROP TABLE IF EXISTS pg_temp._load_ids")
        cur.execute("CREATE TEMP TABLE pg_temp._load_ids (id text) ON COMMIT DROP")
        source_file_set: set[str] = set()
        with cur.copy("COPY pg_temp._load_ids (id) FROM STDIN") as cp:
            for ids, sfs in _iter_id_batches(staging_path, fmt, batch_size):
                for i in ids:
                    cp.write_row((i,))
                source_file_set.update(sfs)
        cur.execute("ANALYZE pg_temp._load_ids")
        source_files = sorted(source_file_set)

        # (c) article_body first. article_body has no source_file column, so
        # target it (c1) by the linkage articles.id == article_body.article_id
        # for the source_file(s) being replaced -- this also removes rows whose
        # id is dropped by a shrinking re-load (which the articles delete below
        # would otherwise orphan) -- then (c2) by the incoming shard's ids for
        # the belt-and-braces case of a pre-existing body row for a new id.
        # Two statements instead of one OR-ed predicate so each can use a join;
        # (c2) only sees what (c1) left, so the sum is the OR-ed delete's count.
        # scoped by source: source_file basenames are not globally unique across sources.
        # (c1) also requires b.source = source: the former OR-ed form did not on
        # its linkage arm, so it could delete another source's body row that
        # shared an id with a replaced article.
        cur.execute(
            """
            DELETE FROM episteme.article_body b
             USING episteme.articles a
             WHERE a.source = %s AND a.source_file = ANY(%s)
               AND b.article_id = a.id AND b.source = %s
            """,
            (source, source_files, source),
        )
        body_deleted = cur.rowcount
        cur.execute(
            "DELETE FROM episteme.article_body "
            "WHERE source = %s AND article_id IN (SELECT id FROM pg_temp._load_ids)",
            (source,),
        )
        body_deleted += cur.rowcount

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
            "WHERE id IN (SELECT id FROM pg_temp._load_ids) AND source = %s "
            "AND NOT (source_file = ANY(%s))",
            (source, source_files),
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

        # (e)+(f) stream the rows: per batch, COPY the hot columns into
        # episteme.articles, then the text columns into episteme.article_body.
        hot_cols_sql = ", ".join(HOT_COLUMNS)
        body_cols_sql = ", ".join(BODY_COLUMNS)
        per_file_inserted: Counter[str] = Counter()
        total = 0
        read_sec = 0.0
        copy_sec = 0.0
        with closing(_iter_batches(staging_path, fmt, batch_size)) as batches:
            while True:
                t0 = time.perf_counter()
                cols = next(batches, None)
                if cols is None:
                    read_sec += time.perf_counter() - t0
                    break
                hot, body = _batch_tuples(cols)
                t1 = time.perf_counter()
                read_sec += t1 - t0
                with cur.copy(f"COPY episteme.articles ({hot_cols_sql}) FROM STDIN") as cp:
                    for row in hot:
                        cp.write_row(row)
                with cur.copy(f"COPY episteme.article_body ({body_cols_sql}) FROM STDIN") as cp:
                    for row in body:
                        cp.write_row(row)
                copy_sec += time.perf_counter() - t1
                per_file_inserted.update(cols["source_file"])
                total += len(cols["id"])

        # (g) one _lineage row per distinct source_file (pmc shards have one).
        for sf in source_files:
            cur.execute(
                """
                INSERT INTO episteme._lineage
                    (source, source_file, run_id, input_content_hash,
                     rows_inserted, rows_deleted)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (source, sf, run_id, content_hash, per_file_inserted[sf], per_file_deleted[sf]),
            )

    elapsed = time.perf_counter() - t_start

    # (h) audit -- in conn's transaction, no commit.
    event = "load_replace" if deleted else "load_commit"
    audit_trail.record(
        event,
        conn=conn,
        object=f"{source} {staging_path.name}",
        input_content_hash=content_hash,
        rows_affected=total,
        run_id=run_id,
    )

    return {
        "source_file": source_files,
        "rows": total,
        "deleted": deleted,
        "body_deleted": body_deleted,
        "event": event,
        "staging_path": str(staging_path),
        "read_sec": round(read_sec, 3),
        "copy_sec": round(copy_sec, 3),
        "rows_per_sec": round(total / elapsed, 1) if elapsed > 0 else 0.0,
    }


def retire_source_files(
    conn,
    *,
    source: str,
    keep_source_files: list[str],
    run_id: str | None = None,
    scope_prefix: str | None = None,
) -> int:
    """Delete every ``source``-scoped row whose ``source_file`` is NOT in
    ``keep_source_files`` -- the retire step for a structured source whose
    raw tree only ever surfaces the CURRENT release per package (SP7's
    ``discover_cdisc_ct_files``). ``load_source_file`` only ever DELETE+COPYs
    the source_file(s) it is called with, so a superseded release's rows are
    never touched by an ordinary load -- this function is what actively
    retires them once the caller has confirmed the new release loaded.

    One transaction; the caller commits. Deletes episteme.article_body rows
    (by the retired articles.id, scoped by source) THEN episteme.articles
    rows (``WHERE source = %s AND NOT (source_file = ANY(keep_source_files))``),
    records one ``load_replace`` audit event, and returns the count of
    deleted ``episteme.articles`` rows. Every delete below is scoped
    ``source = %s`` -- this never touches another source's rows, even one
    sharing a ``source_file`` basename or an ``id`` (source_file basenames
    and ids are not guaranteed globally unique across sources).

    ``scope_prefix``, when given, additionally restricts BOTH deletes to
    ``source_file`` values starting with it (``starts_with()``, not
    ``LIKE`` -- a ``source_file`` may contain ``_``, which ``LIKE`` would
    treat as a wildcard). This is what makes a per-package retire safe for a
    multi-package source like ``cdisc_ct``: without it, a ``keep_source_files``
    covering only ONE package's current release (e.g. because only that
    package was re-downloaded on this machine) would delete every OTHER
    package's rows too, since they are equally "not in the keep set" -- a
    real data-loss bug fixed here (SP7 Task 4 review round 1). The caller
    (``cdisc_ct.retire``) passes one package's ``f"{package}__"`` prefix and
    that package's own current ``source_file`` as the sole keep entry, once
    per discovered package, so a package never touched by a given retire run
    is scoped out entirely rather than relying on it happening to be absent
    from ``keep_source_files``.

    Raises ``ValueError`` if ``keep_source_files`` is empty -- an empty
    keep-set would retire (delete) EVERY row of ``source`` (within
    ``scope_prefix``, if given), almost certainly a caller bug (e.g.
    discovery finding nothing due to a raw-dir typo) than an intentional
    full wipe. Raised before touching the connection at all, so nothing is
    deleted.
    """
    if not keep_source_files:
        raise ValueError(
            f"keep_source_files must not be empty (refusing to retire every {source!r} row)"
        )

    scope_sql = ""
    scope_params: tuple = ()
    if scope_prefix is not None:
        scope_sql = " AND starts_with(source_file, %s)"
        scope_params = (scope_prefix,)

    with conn.cursor() as cur:
        # article_body first (its PK is (article_id, source), no source_file
        # column) -- delete the body of every retired article, EXCEPT an id
        # that also appears under a KEPT source_file for this source (belt-
        # and-braces: load_source_file's own id-collision guard already
        # prevents this in the cdisc_ct pipeline, since an id shared across
        # releases gets replaced under the new source_file before retire ever
        # runs, but retire_source_files is general-purpose and must not ever
        # delete a kept row's only body). scope_sql narrows the "candidates
        # for deletion" subquery only -- the EXCEPT (kept ids) subquery needs
        # no scoping, since it is already exactly keep_source_files.
        cur.execute(
            f"""
            DELETE FROM episteme.article_body
             WHERE source = %s
               AND article_id IN (
                     SELECT id FROM episteme.articles
                      WHERE source = %s AND NOT (source_file = ANY(%s)){scope_sql}
                     EXCEPT
                     SELECT id FROM episteme.articles
                      WHERE source = %s AND source_file = ANY(%s)
                   )
            """,
            (source, source, keep_source_files, *scope_params, source, keep_source_files),
        )

        # articles: everything not in the keep set (and, if scoped, not in
        # scope_prefix -- so an out-of-scope package's stale rows are simply
        # never candidates, regardless of keep_source_files).
        cur.execute(
            "DELETE FROM episteme.articles"
            f" WHERE source = %s AND NOT (source_file = ANY(%s)){scope_sql}",
            (source, keep_source_files, *scope_params),
        )
        deleted = cur.rowcount

    # audit -- in conn's transaction, no commit.
    audit_trail.record(
        "load_replace",
        conn=conn,
        object=f"{source} retire" + (f" {scope_prefix}" if scope_prefix else ""),
        rows_affected=deleted,
        run_id=run_id,
    )

    return deleted
