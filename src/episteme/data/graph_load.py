"""Load a source's per-relation graph Parquet files into the graph tables.

One raw input file (``source_file`` = its ``input_key``) is one transaction:

1. refuse a success marker without graph info (pre-1.6 shard) or a missing relation file;
2. skip when ``_graph_lineage`` already holds the same ``graph_sha256`` (unless ``force``);
3. if the file was graphed before, delete its rows from the six edge tables and
   ``article_facets`` by ``source_file``;
4. COPY every relation file (column-wise Arrow batches), checking the count against the
   marker;
5. derive that file's MeSH facets (``FACET_SQL``);
6. audit (``force_override`` when forced, always ``graph_commit``), upsert lineage, commit.

A failure rolls the file back alone. ``build_pubmed_graph`` retries a transient error
on a fresh connection (audit mirror deferred until the commit) and stops at the first
permanent failure: later files are not touched and the failed file is reported in
``failed`` / ``stopped_at``. The skip state lives in ``_graph_lineage``, in the same
database the deletes act on.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from episteme import audit_trail
from episteme.data.checkpoint_markers import ops_root
from episteme.data.graph_files import (
    RELATION_COLUMNS,
    RELATIONS,
    graph_file_path,
    marker_graph_info,
)
from episteme.ops.profile import default_batch_rows
from episteme.ops.retry import RETRY_WAITS, with_retries

_BATCH_ROWS = default_batch_rows()
_SLEEP = time.sleep  # seam: tests patch this so retry waits cost nothing


class GraphLoadError(RuntimeError):
    """A source file (or the whole run) cannot be graph-loaded."""


TABLE_FOR: dict[str, str] = {
    "mesh": "episteme.article_mesh",
    "cites": "episteme.article_cites",
    "comments": "episteme.article_comments",
    "chemicals": "episteme.article_chemicals",
    "keywords": "episteme.article_keywords",
    "authors": "episteme.article_authors",
}

FACET_SQL = """
WITH rv AS (SELECT max(rule_version) v FROM episteme.mesh_facet_rule),
r AS (SELECT r.* FROM episteme.mesh_facet_rule r, rv WHERE r.rule_version = rv.v),
hits AS (
  SELECT m.pmid, r.facet, m.descriptor_ui, r.rule_version
    FROM episteme.article_mesh m JOIN r ON r.kind = 'descriptor' AND r.value = m.descriptor_ui
   WHERE m.source_file = %(sf)s
  UNION
  SELECT m.pmid, r.facet, m.descriptor_ui, r.rule_version
    FROM episteme.article_mesh m
    JOIN episteme.mesh_tree t ON t.descriptor_ui = m.descriptor_ui
    JOIN r ON r.kind = 'tree_prefix'
          AND (t.tree_number = r.value OR t.tree_number LIKE r.value || '.%%'
               OR (length(r.value) = 1 AND t.tree_number LIKE r.value || '%%'))
   WHERE m.source_file = %(sf)s
  UNION
  SELECT c.pmid, 'drug', c.substance_ui, rv.v
    FROM episteme.article_chemicals c, rv
   WHERE c.source_file = %(sf)s
     AND EXISTS (SELECT 1 FROM episteme.mesh_tree t
                  WHERE t.descriptor_ui = c.substance_ui AND t.tree_number LIKE 'D%%')
)
INSERT INTO episteme.article_facets (pmid, facet, descriptor_ui, rule_version, source_file)
SELECT pmid, facet, descriptor_ui, rule_version, %(sf)s FROM hits
"""

_REEXTRACT = "no graph files in its marker; re-extract with schema >= 1.6"


def _success_markers(processed_dir: Path, source: str) -> tuple[list[str], list[str], list[str]]:
    """``(graphed, no_graph_info, corrupt)`` source files from the success markers, each
    sorted. ``corrupt`` are markers that are not valid JSON (e.g. truncated)."""
    success = ops_root(processed_dir, source) / "success"
    if not success.is_dir():
        return [], [], []
    graphed: list[str] = []
    old: list[str] = []
    corrupt: list[str] = []
    for p in success.glob("*.ok"):
        sf = p.name[: -len(".ok")]
        try:
            info = marker_graph_info(processed_dir, source, sf)
        except ValueError:  # JSONDecodeError is a ValueError
            corrupt.append(sf)
            continue
        (graphed if info is not None else old).append(sf)
    return sorted(graphed), sorted(old), sorted(corrupt)


def graphed_files(processed_dir: Path, source: str) -> list[str]:
    """The ``source_file``s whose success marker carries graph info, sorted."""
    return _success_markers(processed_dir, source)[0]


def _copy_relation(cur, rel: str, path: Path, source_file: str) -> int:
    import pyarrow.parquet as pq

    cols = RELATION_COLUMNS[rel]
    sf_idx = cols.index("source_file")
    n = 0
    with cur.copy(f"COPY {TABLE_FOR[rel]} ({', '.join(cols)}) FROM STDIN") as cp:
        for batch in pq.ParquetFile(path).iter_batches(batch_size=_BATCH_ROWS, columns=cols):
            columns = [batch.column(i).to_pylist() for i in range(len(cols))]
            # Key every row on the load's source_file, so the per-file delete, the facets
            # and the lineage row always agree with what was copied.
            columns[sf_idx] = [source_file] * batch.num_rows
            for row in zip(*columns, strict=True):
                cp.write_row(row)
            n += batch.num_rows
    return n


def load_one(
    conn,
    *,
    source: str,
    source_file: str,
    processed_dir: Path,
    run_id: str,
    force: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    """Graph-load one source file in its own transaction. Commits on success (through
    ``audit_trail.commit``, so a deferred audit mirror is written after the commit);
    rolls back and re-raises on any error."""
    try:
        if force and not reason:
            raise GraphLoadError("--force requires --reason")
        info = marker_graph_info(processed_dir, source, source_file)
        if info is None:
            raise GraphLoadError(f"{source_file}: {_REEXTRACT}")
        paths: dict[str, Path] = {}
        for rel in RELATIONS:
            p = graph_file_path(processed_dir, source, rel, source_file)
            if not p.is_file():
                raise GraphLoadError(f"{source_file}: graph file missing for relation {rel!r}")
            paths[rel] = p

        with conn.cursor() as cur:
            cur.execute(
                "SELECT graph_sha256 FROM episteme._graph_lineage "
                "WHERE source = %s AND source_file = %s",
                (source, source_file),
            )
            prev = cur.fetchone()
        if prev is not None and prev[0] == info["graph_sha256"] and not force:
            audit_trail.rollback(conn)  # end the read-only transaction; nothing was written
            return {"source_file": source_file, "skipped": True, "rows": {}, "facets": 0}

        rows: dict[str, int] = {}
        with conn.cursor() as cur:
            if prev is not None:
                for table in (*TABLE_FOR.values(), "episteme.article_facets"):
                    cur.execute(f"DELETE FROM {table} WHERE source_file = %s", (source_file,))
            for rel in RELATIONS:
                n = _copy_relation(cur, rel, paths[rel], source_file)
                expected = info["graph_rows"].get(rel)
                if expected is not None and n != expected:
                    raise GraphLoadError(
                        f"{source_file}: relation {rel!r} copied {n} rows, marker says {expected}"
                    )
                rows[rel] = n
            cur.execute(FACET_SQL, {"sf": source_file})
            facets = cur.rowcount

        if force:
            audit_trail.record(
                "force_override",
                conn=conn,
                object=f"{source} graph {source_file}",
                reason=reason,
                run_id=run_id,
            )
        audit_trail.record(
            "graph_commit",
            conn=conn,
            object=f"{source} {source_file}",
            input_content_hash=info["graph_sha256"],
            rows_affected=sum(rows.values()) + facets,
            new_value={"rows": rows, "facets": facets},
            run_id=run_id,
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO episteme._graph_lineage "
                "(source, source_file, graph_sha256, rows, run_id) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (source, source_file) DO UPDATE SET "
                "graph_sha256 = EXCLUDED.graph_sha256, rows = EXCLUDED.rows, "
                "run_id = EXCLUDED.run_id, graphed_at = now()",
                (source, source_file, info["graph_sha256"], Jsonb(rows), run_id),
            )
        audit_trail.commit(conn)
    except BaseException as exc:
        if not isinstance(exc, Exception):
            # A stop (KeyboardInterrupt/SystemExit): cancel the running statement first.
            try:
                conn.cancel_safe()
            except Exception:  # noqa: BLE001 - best effort
                pass
        try:
            audit_trail.rollback(conn)
        except Exception:  # noqa: BLE001 - keep the original error (a stop must stay a stop)
            pass
        raise
    return {"source_file": source_file, "skipped": False, "rows": rows, "facets": facets}


def build_pubmed_graph(
    conn,
    *,
    processed_dir: Path,
    run_id: str,
    force: bool = False,
    reason: str | None = None,
    holder: list | None = None,
) -> dict[str, Any]:
    """Graph-load every extracted pubmed file, one transaction each; stop at the first
    permanent failure.

    ``conn`` serves only the start-up check; ``None`` runs it on its own short
    connection that is released before the first file loads. Each file loads on a fresh connection
    inside ``audit_trail.deferred_mirror`` and is retried on a transient error
    (``with_retries``). ``holder`` (optional one-item list) receives the connection of
    the attempt in progress so a caller's ``stage_guard`` can cancel it.

    Returns ``{files, loaded, skipped, failed, rows, facets}``. The first permanent
    failure (a file with a pre-1.6 or unreadable marker, or a load error) is printed to
    stderr, ends the loop and is the only entry of ``failed``; ``stopped_at`` names it
    and ``error`` holds its ``repr``. ``old`` / ``corrupt`` markers are checked first,
    before any file loads. Later files are not touched.
    """
    source = "pubmed"
    processed_dir = Path(processed_dir)
    if force and not reason:
        raise GraphLoadError("--force requires --reason")
    # Lazy import: a module-level binding would freeze whatever `connection` was
    # importable first (tests stub it).
    from episteme.data.db.connection import connection

    def _has_tree(c) -> bool:
        with c.cursor() as cur:
            cur.execute("SELECT 1 FROM episteme.mesh_tree LIMIT 1")
            found = cur.fetchone() is not None
        c.rollback()
        return found

    if conn is not None:
        has_tree = _has_tree(conn)
    else:
        with connection() as short:
            has_tree = _has_tree(short)
    if not has_tree:
        raise GraphLoadError("mesh_tree is empty; run `run_pipeline.sh mesh graph` first")

    graphed, old, corrupt = _success_markers(processed_dir, source)
    out: dict[str, Any] = {
        "files": len(graphed) + len(old) + len(corrupt),
        "loaded": 0,
        "skipped": 0,
        "failed": [],
        "rows": {rel: 0 for rel in RELATIONS},
        "facets": 0,
    }

    def _stop(sf: str, exc: Exception) -> dict[str, Any]:
        print(f"ERROR: graph load {sf}: {type(exc).__name__}: {exc}", file=sys.stderr)
        out["failed"] = [sf]
        out["stopped_at"] = sf
        out["error"] = repr(exc)
        return out

    for sf in old:
        return _stop(sf, GraphLoadError(f"{sf}: {_REEXTRACT}"))
    for sf in corrupt:
        return _stop(sf, GraphLoadError(f"{sf}: unreadable success marker; re-extract the file"))

    def _attempt(sf: str) -> dict[str, Any]:
        """One attempt: a fresh connection, one transaction, one commit."""
        with connection() as uconn:
            if holder is not None:
                holder[0] = uconn
            try:
                with audit_trail.deferred_mirror(uconn):
                    return load_one(
                        uconn,
                        source=source,
                        source_file=sf,
                        processed_dir=processed_dir,
                        run_id=run_id,
                        force=force,
                        reason=reason,
                    )
            finally:
                if holder is not None:
                    holder[0] = None

    def _on_retry(sf: str):
        def _cb(attempt: int, exc: BaseException, wait: float) -> None:
            print(f"retry {sf} ({attempt}/{len(RETRY_WAITS)}) in {wait}s: {exc!r}")

        return _cb

    for sf in graphed:
        try:
            res = with_retries(
                lambda sf=sf: _attempt(sf),
                sleep=lambda secs: _SLEEP(secs),
                on_retry=_on_retry(sf),
            )
        except Exception as e:  # noqa: BLE001 - permanent: stop the run at this file
            return _stop(sf, e)
        if res["skipped"]:
            out["skipped"] += 1
            continue
        out["loaded"] += 1
        for rel, n in res["rows"].items():
            out["rows"][rel] += n
        out["facets"] += res["facets"]
    return out


_STALE_SQL = """
WITH rv AS (SELECT max(rule_version) v FROM episteme.mesh_facet_rule)
SELECT rv.v,
       count(f.*) FILTER (WHERE f.rule_version < rv.v),
       count(DISTINCT f.source_file) FILTER (WHERE f.rule_version < rv.v)
  FROM rv LEFT JOIN episteme.article_facets f ON true
 GROUP BY rv.v
"""

_FACET_FILES_SQL = """
SELECT source_file FROM episteme._graph_lineage WHERE source = 'pubmed' ORDER BY 1
"""


def facet_status(conn) -> dict[str, int]:
    """Newest rule version and how many facet rows / files are stale against it."""
    with conn.cursor() as cur:
        cur.execute(_STALE_SQL)
        row = cur.fetchone()
        cur.execute(_FACET_FILES_SQL)
        files = len(cur.fetchall())
    conn.rollback()  # read-only; end the transaction
    return {
        "rule_version": row[0] if row and row[0] is not None else 0,
        "stale_rows": row[1] if row else 0,
        "stale_files": row[2] if row else 0,
        "files": files,
    }


def rebuild_facets(
    conn, *, run_id: str, force: bool = False, reason: str | None = None
) -> dict[str, int]:
    """Re-derive facets per source file under the newest rule version.

    A file is skipped when it has facet rows and none is stale (unless ``force``).
    Each rebuilt file is one committed transaction, so the run is restartable.
    """
    if force and not reason:
        raise GraphLoadError("--force requires --reason")
    with conn.cursor() as cur:
        cur.execute("SELECT max(rule_version) FROM episteme.mesh_facet_rule")
        newest = cur.fetchone()[0]
        cur.execute(_FACET_FILES_SQL)
        files = [r[0] for r in cur.fetchall()]
        cur.execute(
            "SELECT source_file, bool_or(rule_version < %s) FROM episteme.article_facets "
            "GROUP BY source_file",
            (newest,),
        )
        stale_by_file = dict(cur.fetchall())
    conn.rollback()
    out = {"rebuilt": 0, "skipped": 0}
    for sf in files:
        if not force and stale_by_file.get(sf) is False:
            out["skipped"] += 1
            continue
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM episteme.article_facets WHERE source_file = %s", (sf,))
                cur.execute(FACET_SQL, {"sf": sf})
                facets = cur.rowcount
            if force:
                audit_trail.record(
                    "force_override",
                    conn=conn,
                    object=f"facets {sf}",
                    reason=reason,
                    run_id=run_id,
                )
            audit_trail.record(
                "graph_commit",
                conn=conn,
                object=f"facets {sf}",
                rows_affected=facets,
                new_value={"rule_version": newest, "facets": facets},
                run_id=run_id,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        out["rebuilt"] += 1
    return out
