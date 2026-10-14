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

A failure rolls the file back alone; ``build_pubmed_graph`` reports it and moves on.
The skip state lives in ``_graph_lineage``, in the same database the deletes act on.
"""

from __future__ import annotations

import sys
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

_BATCH_ROWS = 50_000


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


def _success_markers(processed_dir: Path, source: str) -> tuple[list[str], list[str]]:
    """``(graphed, no_graph_info)`` source files from the success markers, each sorted."""
    success = ops_root(processed_dir, source) / "success"
    if not success.is_dir():
        return [], []
    graphed: list[str] = []
    old: list[str] = []
    for p in success.glob("*.ok"):
        sf = p.name[: -len(".ok")]
        (graphed if marker_graph_info(processed_dir, source, sf) is not None else old).append(sf)
    return sorted(graphed), sorted(old)


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
    """Graph-load one source file in its own transaction. Commits on success; rolls
    back and re-raises on any error."""
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
            conn.rollback()  # end the read-only transaction; nothing was written
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
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {"source_file": source_file, "skipped": False, "rows": rows, "facets": facets}


def build_pubmed_graph(
    conn,
    *,
    processed_dir: Path,
    run_id: str,
    force: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    """Graph-load every extracted pubmed file, one transaction each.

    Returns ``{files, loaded, skipped, failed, rows, facets}``; a failed file is rolled
    back, printed to stderr and listed in ``failed`` while the loop continues.
    """
    source = "pubmed"
    processed_dir = Path(processed_dir)
    if force and not reason:
        raise GraphLoadError("--force requires --reason")
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM episteme.mesh_tree LIMIT 1")
        has_tree = cur.fetchone() is not None
    conn.rollback()
    if not has_tree:
        raise GraphLoadError("mesh_tree is empty; run `run_pipeline.sh mesh graph` first")

    graphed, old = _success_markers(processed_dir, source)
    out: dict[str, Any] = {
        "files": len(graphed) + len(old),
        "loaded": 0,
        "skipped": 0,
        "failed": [],
        "rows": {rel: 0 for rel in RELATIONS},
        "facets": 0,
    }
    for sf in old:
        print(f"ERROR: graph load {sf}: {_REEXTRACT}", file=sys.stderr)
        out["failed"].append(sf)
    for sf in graphed:
        try:
            res = load_one(
                conn,
                source=source,
                source_file=sf,
                processed_dir=processed_dir,
                run_id=run_id,
                force=force,
                reason=reason,
            )
        except Exception as e:  # noqa: BLE001 - one file's failure must not stop the run
            conn.rollback()
            print(f"ERROR: graph load {sf}: {type(e).__name__}: {e}", file=sys.stderr)
            out["failed"].append(sf)
            continue
        if res["skipped"]:
            out["skipped"] += 1
            continue
        out["loaded"] += 1
        for rel, n in res["rows"].items():
            out["rows"][rel] += n
        out["facets"] += res["facets"]
    out["failed"].sort()
    return out
