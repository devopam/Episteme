"""Populate the property-graph edge tables from loaded articles + raw PMC JATS.

``build`` reads ``episteme.articles``. For source ``pmc`` it locates each
article's raw JATS XML under ``raw_dir`` and derives two edge sets per
``source_file``:

  * ``episteme.article_cites`` -- ``(src_pmid, dst_pmid)`` from every
    ``<pub-id pub-id-type="pmid">`` under ``<ref-list>``.
  * ``episteme.article_mesh`` -- one row per ``<kwd>`` under any ``<kwd-group>``.
    PMC OA JATS carries no MeSH descriptors (``extract_pmc`` sets
    ``articles.mesh = None``), so the keyword group is the only signal;
    ``descriptor_ui`` is left NULL for these keyword-derived (non-MeSH) rows,
    and ``descriptor_name`` carries the keyword text; ``major_topic`` /
    ``qualifiers`` are ``NULL``. ``neighbours(kind="mesh")`` falls back to
    ``descriptor_name`` when ``descriptor_ui`` is absent.

``pubmed`` is routed to ``graph_load.build_pubmed_graph``: it loads the
extractor's per-relation graph files (mesh, cites, comments, chemicals,
keywords, authors) and derives MeSH facets, one committed transaction per
source file. The phases below do not run for pubmed.

For every other SP2 literature source (``pmc``, ``apollo``,
``europepmc_manuscript``, ``europepmc_preprint``, ``guidelines``, ``bookshelf``
-- see ``_GRAPH_SOURCES``) ``build`` also derives a third set, straight from the
loaded rows with no raw XML involved:

  * ``episteme.article_parts`` -- ``(container_id, part_id)`` book -> part
    edges, one row per ``episteme.articles`` row whose ``container_id`` is set,
    keyed to the part row's ``source_file``.

For source ``mesh`` (SP4), ``build`` derives a fourth set, re-parsing the raw
MeSH descriptor release XML under ``raw_dir`` (matched to ``mesh`` articles
rows' distinct ``source_file``s) rather than persisting a new "extra fields"
column on ``articles`` -- same architectural pattern as the pmc cites/mesh
phase above:

  * ``episteme.mesh_hierarchy`` -- ``(parent_descriptor_ui, child_descriptor_ui)``
    edges, computed by tree-number-prefix matching within one release file's
    full descriptor set (a MeSH release ships its whole vocabulary in one
    file, so this needs no cross-file join).

Idempotency is DELETE-by-``source_file`` + INSERT (the cites/mesh tables have no
primary key; ``article_parts``/``mesh_hierarchy`` additionally guard with
``ON CONFLICT ... DO NOTHING``). ``build`` runs inside the caller's transaction
and never commits -- matching ``postgres_loader``. Each processed
``source_file`` gets one ``audit_trail.record("graph_commit", ...)`` row.

Articles with no ``pmid`` are counted and skipped (``skipped_no_pmid`` in the
returned dict) rather than being silently excluded by the query.

``neighbours`` answers a hop-bounded reachability question with two back ends:
a recursive-CTE walk (the DEFAULT and always-tested path) and a SQL/PGQ
``GRAPH_TABLE`` path that is only reached when the ``episteme_graph`` property
graph actually exists (it does NOT on the PG19beta3 build here -- ``schema.sql``
lands on a NOTICE). ``_pgq_available`` probes ``pg_catalog.pg_class`` for it.

XML is parsed with ``defusedxml`` (entity-expansion safe); JATS namespaces are
rare but tags are matched on their local name via ``_local`` for safety.

This module never reads ``os.environ`` -- config comes from ``episteme.config``
via ``audit_trail``.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import defusedxml.ElementTree as ET
import psycopg

from episteme import audit_trail
from episteme.data import graph_load
from episteme.data.checkpoint_markers import find_input_by_key
from episteme.data.gz import open_gz
from episteme.data.pubmed.relations import normalize_orcid
from episteme.ops import orphans
from episteme.ops import status as ops_status
from episteme.ops.failure import record_failure
from episteme.ops.retry import RETRY_WAITS, with_retries
from episteme.ops.stop import stage_guard

_SLEEP = time.sleep  # seam: tests patch this so retry waits cost nothing

# Sources ``build`` knows how to graph. Only ``pmc`` has raw JATS to parse for
# cites/mesh; ``pubmed`` loads graph files (``graph_load``); the rest contribute
# ``article_parts`` (book -> part) edges only.
# Anything outside this set is still ``NotImplementedError`` (e.g. ``chembl``).
_GRAPH_SOURCES = (
    "pmc",
    "pubmed",
    "apollo",
    "europepmc_manuscript",
    "europepmc_preprint",
    "guidelines",
    "bookshelf",
    "mesh",
)

_CITES_INSERT = (
    "INSERT INTO episteme.article_cites (src_pmid, dst_pmid, source_file) VALUES (%s, %s, %s)"
)
_MESH_INSERT = (
    "INSERT INTO episteme.article_mesh "
    "(pmid, descriptor_ui, descriptor_name, major_topic, qualifiers, source_file) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)
_PARTS_INSERT = (
    "INSERT INTO episteme.article_parts (container_id, part_id, source_file) "
    "VALUES (%s, %s, %s) ON CONFLICT (container_id, part_id) DO NOTHING"
)
_MESH_HIERARCHY_INSERT = (
    "INSERT INTO episteme.mesh_hierarchy (parent_descriptor_ui, child_descriptor_ui, source_file) "
    "VALUES (%s, %s, %s) ON CONFLICT (parent_descriptor_ui, child_descriptor_ui) DO NOTHING"
)

_LOG = logging.getLogger(__name__)


def _local(tag: str) -> str:
    """Strip any ``{namespace}`` prefix from an ElementTree tag."""
    return tag.rsplit("}", 1)[-1] if tag else tag


def _version_rank(version: str) -> tuple[tuple[int, int, str], ...]:
    """Sort key for a dotted version such as ``2.1``: one entry per component.

    A numeric component ranks ``(1, value, "")``; a non-numeric one ranks
    ``(0, 0, text)``, below every number and ordered by its text, so the result
    is deterministic. ``"2"`` sorts below ``"2.1"`` (a longer tuple wins a tie).
    """
    return tuple(
        (1, int(part), "") if part.isdigit() else (0, 0, part) for part in version.split(".")
    )


def _xml_index(raw_dir: Path) -> dict[str, Path]:
    """Walk ``raw_dir`` once; map each XML file stem (``PMC1.2`` for ``PMC1.2.xml``,
    the version id) to its path. Real data holds about 935k XML files, so this is
    the only walk per build.

    The PMC row's ``source_record_id`` is the version id, so the graph reads the
    exact version that was loaded. When several files share a stem, the path
    whose parent is ``raw_dir/xml`` wins (the extractor's shared-folder lookup),
    then the smallest relative path. An unreadable directory is logged and the
    walk goes on; an unreadable ``raw_dir`` is logged as an error.
    """
    shared = os.path.join(str(raw_dir), "xml")
    best: dict[str, tuple[bool, str, str]] = {}
    root = str(raw_dir)
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError as exc:
            if current == root:
                _LOG.error("graph: cannot read the raw directory %s: %s", current, exc)
            else:
                _LOG.warning("graph: cannot read directory %s: %s", current, exc)
            continue
        with it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                    continue
                name = entry.name
                if not name.endswith(".xml"):
                    continue
                stem = name[:-4]
                rank = (current != shared, os.path.relpath(entry.path, root))
                prev = best.get(stem)
                if prev is None or rank < (prev[0], prev[1]):
                    best[stem] = (rank[0], rank[1], entry.path)
    return {stem: Path(path) for stem, (_, _, path) in best.items()}


def _find_xml(index: dict[str, Path], version_id: str | None) -> Path | None:
    """The XML for one version id (``PMC1.1``) from ``_xml_index``; ``None`` when
    the id is empty or no file carries that exact stem."""
    if not version_id:
        return None
    return index.get(version_id)


def _pmc_rows_by_file(
    conn, batch_rows: int = 5_000
) -> Iterator[tuple[str, list[tuple[str, str | None, str | None, str | None]]]]:
    """Yield ``(source_file, [(id, pmid, pmcid, source_record_id), ...])``, one
    source_file at a time.

    Reads through a named (server-side) cursor, ``batch_rows`` rows per fetch, so
    the full pmc row set is never held in memory. Needs the caller's open
    transaction (``build`` never commits).
    """
    current: str | None = None
    rows: list[tuple[str, str | None, str | None, str | None]] = []
    with conn.cursor(name="episteme_graph_pmc_rows") as cur:
        cur.execute(
            "SELECT id, pmid, pmcid, source_file, source_record_id FROM episteme.articles "
            "WHERE source = 'pmc' ORDER BY source_file, id"
        )
        while True:
            batch = cur.fetchmany(batch_rows)
            if not batch:
                break
            for _id, pmid, pmcid, source_file, record_id in batch:
                if source_file != current:
                    if current is not None:
                        yield current, rows
                    current, rows = source_file, []
                rows.append((_id, pmid, pmcid, record_id))
    if current is not None:
        yield current, rows


def _parse_jats(xml_path: Path) -> tuple[list[str], list[str]]:
    """Return ``(dst_pmids, keywords)`` from one JATS file.

    ``dst_pmids`` = stripped text of every ``<pub-id pub-id-type="pmid">``
    inside a ``<ref-list>``. ``keywords`` = stripped text of every ``<kwd>``
    inside any ``<kwd-group>``. Neither list is de-duplicated here; the caller
    de-dups per ``source_file``. A parse failure yields two empty lists.
    """
    try:
        root = ET.parse(str(xml_path)).getroot()
    except Exception:  # noqa: BLE001 -- a malformed shard must not abort the build
        return [], []

    dst_pmids: list[str] = []
    for el in root.iter():
        if _local(el.tag) != "ref-list":
            continue
        for sub in el.iter():
            if _local(sub.tag) != "pub-id":
                continue
            if (sub.get("pub-id-type") or "").strip().lower() != "pmid":
                continue
            txt = (sub.text or "").strip()
            if txt:
                dst_pmids.append(txt)

    keywords: list[str] = []
    for el in root.iter():
        if _local(el.tag) != "kwd-group":
            continue
        for sub in el.iter():
            if _local(sub.tag) != "kwd":
                continue
            txt = "".join(sub.itertext()).strip()
            if txt:
                keywords.append(txt)

    return dst_pmids, keywords


def _open_mesh_source(xml_path: Path):
    """Binary file handle for ``xml_path`` -- transparently gzip-decompressing
    when its suffix is ``.gz``. Mirrors ``serialize_mesh.py``'s own
    ``_open_source`` helper (same real-world ``desc<year>.gz`` shape)."""
    return open_gz(xml_path, "rb") if xml_path.suffix == ".gz" else xml_path.open("rb")


def _parse_mesh_descriptors(xml_path: Path) -> dict[str, list[str]]:
    """Return {descriptor_ui: [tree_number, ...]} for every
    <DescriptorRecord> in one MeSH descriptor release file (bare ``.xml`` or
    gzip-compressed ``.gz``/``.xml.gz`` -- transparently decompressed via
    ``_open_mesh_source``, the same real-world shape ``serialize_mesh.py``
    discovers and writes into ``source_file``). Streamed via
    ``ET.iterparse`` (``ET`` IS ``defusedxml.ElementTree``, same streaming
    discipline ``serialize_mesh.py``'s ``_iter_descriptor_records`` already
    established for this exact file shape -- SP2's 821MB-file precedent), so
    memory stays flat regardless of file size. A parse failure (malformed
    XML, corrupt gzip, ...) yields an empty dict (mirrors _parse_jats's
    failure mode)."""
    out: dict[str, list[str]] = {}
    try:
        with _open_mesh_source(xml_path) as fh:
            context = iter(ET.iterparse(fh, events=("start", "end")))
            _, root = next(context)
            for event, elem in context:
                if event != "end" or _local(elem.tag) != "DescriptorRecord":
                    continue
                ui = None
                trees: list[str] = []
                for el in elem.iter():
                    tag = _local(el.tag)
                    if tag == "DescriptorUI" and ui is None:
                        ui = (el.text or "").strip()
                    elif tag == "TreeNumber":
                        txt = (el.text or "").strip()
                        if txt:
                            trees.append(txt)
                if ui:
                    out[ui] = trees
                elem.clear()
                root.clear()
    except Exception:  # noqa: BLE001 -- a malformed file must not abort the build
        return {}
    return out


def build(
    conn,
    *,
    source: str,
    raw_dir: Path | str | None,
    run_id: str,
    processed_dir: Path | str | None = None,
    force: bool = False,
    reason: str | None = None,
    holder: list | None = None,
) -> dict[str, Any]:
    """Rebuild the edge tables for ``source`` from loaded rows (+ raw JATS for pmc,
    raw MeSH descriptor XML for mesh).

    One transaction; the caller commits. Three phases:

    * **cites + mesh (``pmc`` only).** For every distinct ``source_file`` in
      ``episteme.articles`` (rows with no ``pmid`` are counted in
      ``skipped_no_pmid`` and excluded): DELETE its rows from both edge tables,
      re-derive them from the raw JATS under ``raw_dir``, ``executemany`` them
      back, and write one ``graph_commit`` audit row. For non-pmc sources this
      phase is skipped and ``cites``/``mesh``/``missing_xml``/``skipped_no_pmid``
      stay ``0`` and ``source_files`` stays ``[]``.
    * **parts (every ``_GRAPH_SOURCES`` source).** DELETE-by-``source_file`` then
      re-INSERT one ``episteme.article_parts`` row per loaded ``articles`` row
      whose ``container_id`` is set, ``ON CONFLICT (container_id, part_id) DO
      NOTHING``. One ``graph_commit`` audit row per ``source_file``. (``mesh``
      rows have ``container_id IS NULL``, so this phase naturally contributes
      0 rows for ``source="mesh"`` -- no guard needed.)
    * **mesh_hierarchy (``mesh`` only).** For every distinct ``source_file`` in
      ``episteme.articles`` with ``source='mesh'``: DELETE-by-``source_file``,
      re-parse the matching raw MeSH descriptor XML under ``raw_dir``, derive
      parent/child edges by tree-number-prefix matching, ``executemany`` them
      back ``ON CONFLICT (parent_descriptor_ui, child_descriptor_ui) DO
      NOTHING``, and write one ``graph_commit`` audit row per ``source_file``.
      A ``source_file`` that IS found on disk but parses to zero descriptors
      (malformed XML, corrupt gzip, ...) increments ``mesh_empty_parse`` and
      prints a WARNING to stderr -- distinct from the file simply not
      existing (silently skipped, no warning).

    Returns ``{source_files, cites, mesh, missing_xml, skipped_no_pmid, parts,
    mesh_hierarchy, mesh_tree, mesh_empty_parse}``.

    ``source="pubmed"`` is different: it loads the extractor's per-relation graph
    files from ``processed_dir`` via :func:`graph_load.build_pubmed_graph` (one
    committed transaction per source file; ``raw_dir`` is unused) and returns that
    function's ``{files, loaded, skipped, failed, rows, facets}`` (it stops at the
    first failed file; ``holder`` is forwarded to it).

    Data tolerance is not a unit failure: a malformed JATS or MeSH file inside this
    build is skipped or counted in the result (``# a malformed shard must not abort
    the build``), never raised. Only a database or I/O error fails the build.
    """
    if source not in _GRAPH_SOURCES:
        raise NotImplementedError(f"{source} graph build is SP2+")

    if source == "pubmed":
        if processed_dir is None:
            raise ValueError("processed_dir is required for source 'pubmed'")
        return graph_load.build_pubmed_graph(
            conn,
            processed_dir=Path(processed_dir),
            run_id=run_id,
            force=force,
            reason=reason,
            holder=holder,
        )

    raw_dir = Path(raw_dir)

    skipped_no_pmid = 0
    total_cites = 0
    total_mesh = 0
    missing_xml = 0
    handled: list[str] = []

    if source == "pmc":
        xml_index = _xml_index(raw_dir)

        for source_file, file_rows in _pmc_rows_by_file(conn):
            articles = [(pmid, record_id) for _id, pmid, _pmcid, record_id in file_rows if pmid]
            skipped_no_pmid += len(file_rows) - len(articles)
            if not articles:
                continue

            cites_rows: list[tuple[str, str, str]] = []
            mesh_rows: list[tuple[str, None, str, None, None, str]] = []
            seen_cites: set[tuple[str, str]] = set()
            seen_mesh: set[tuple[str, str]] = set()

            for pmid, record_id in articles:
                xml_path = _find_xml(xml_index, record_id)
                if xml_path is None:
                    missing_xml += 1
                    continue

                dst_pmids, keywords = _parse_jats(xml_path)

                for dst in dst_pmids:
                    if not dst or dst == pmid:
                        continue
                    key = (pmid, dst)
                    if key in seen_cites:
                        continue
                    seen_cites.add(key)
                    cites_rows.append((pmid, dst, source_file))

                for name in keywords:
                    if not name:
                        continue
                    key = (pmid, name)
                    if key in seen_mesh:
                        continue
                    seen_mesh.add(key)
                    mesh_rows.append((pmid, None, name, None, None, source_file))

            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM episteme.article_cites WHERE source_file = %s", (source_file,)
                )
                cur.execute(
                    "DELETE FROM episteme.article_mesh WHERE source_file = %s", (source_file,)
                )
                if cites_rows:
                    cur.executemany(_CITES_INSERT, cites_rows)
                if mesh_rows:
                    cur.executemany(_MESH_INSERT, mesh_rows)

            audit_trail.record(
                "graph_commit",
                conn=conn,
                object=f"pmc {source_file}",
                rows_affected=len(cites_rows) + len(mesh_rows),
                run_id=run_id,
            )

            total_cites += len(cites_rows)
            total_mesh += len(mesh_rows)
            handled.append(source_file)

    # SP2: container -> part edges, read from loaded rows (not raw XML). Runs for
    # every allowed source, pmc included.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT container_id, id, source_file FROM episteme.articles "
            "WHERE source = %s AND container_id IS NOT NULL",
            (source,),
        )
        part_src_rows = cur.fetchall()

    parts_by_file: dict[str, list[tuple[str, str]]] = {}
    for c_id, part_id, src_file in part_src_rows:
        parts_by_file.setdefault(src_file, []).append((c_id, part_id))

    total_parts = 0
    for src_file, pairs in sorted(parts_by_file.items()):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM episteme.article_parts WHERE source_file = %s", (src_file,))
            cur.executemany(
                _PARTS_INSERT,
                [(c_id, part_id, src_file) for c_id, part_id in pairs],
            )
        audit_trail.record(
            "graph_commit",
            conn=conn,
            object=f"{source} {src_file}",
            rows_affected=len(pairs),
            run_id=run_id,
        )
        total_parts += len(pairs)

    # SP4: MeSH descriptor parent/child tree-number edges, re-parsed straight
    # from the raw descriptor release XML (same architectural pattern as the
    # pmc cites/mesh phase above -- no persisted "extra fields" column).
    total_mesh_hierarchy = 0
    total_mesh_tree = 0
    empty_parse = 0
    if source == "mesh":
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT source_file FROM episteme.articles WHERE source = 'mesh'")
            mesh_files = [r[0] for r in cur.fetchall()]

        for src_file in sorted(mesh_files):
            xml_path = find_input_by_key(Path(raw_dir), src_file)
            if xml_path is None:
                continue

            descriptors = _parse_mesh_descriptors(xml_path)
            if not descriptors:
                # The file WAS found on disk but yielded zero descriptors --
                # a genuine "something is wrong" signal (malformed XML,
                # corrupt gzip, unexpected root shape, ...), distinct from
                # "the file simply doesn't exist" (silently `continue`d
                # above). Print, don't hard-fail: build() must still finish
                # the other source_files in this run.
                empty_parse += 1
                print(
                    f"WARNING: graph_builder mesh phase: {xml_path} parsed to zero "
                    f"descriptors (source_file={src_file!r}); mesh_hierarchy for this "
                    "file will be empty -- check the file is valid MeSH descriptor XML",
                    file=sys.stderr,
                )
            # tree number -> descriptor UI, to resolve a child's parent prefix
            tree_to_ui = {t: ui for ui, trees in descriptors.items() for t in trees}

            edges: list[tuple[str, str, str]] = []
            seen: set[tuple[str, str]] = set()
            for ui, trees in descriptors.items():
                for tree in trees:
                    if "." not in tree:
                        continue  # top-level descriptor, no parent
                    parent_tree = tree.rsplit(".", 1)[0]
                    parent_ui = tree_to_ui.get(parent_tree)
                    if not parent_ui or parent_ui == ui:
                        continue
                    key = (parent_ui, ui)
                    if key in seen:
                        continue
                    seen.add(key)
                    edges.append((parent_ui, ui, src_file))

            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM episteme.mesh_hierarchy WHERE source_file = %s", (src_file,)
                )
                if edges:
                    cur.executemany(_MESH_HIERARCHY_INSERT, edges)

            tree_rows = [(ui, t, src_file) for ui, trees in descriptors.items() for t in trees]
            with conn.cursor() as cur:
                cur.execute("DELETE FROM episteme.mesh_tree WHERE source_file = %s", (src_file,))
                with cur.copy(
                    "COPY episteme.mesh_tree (descriptor_ui, tree_number, source_file) FROM STDIN"
                ) as cp:
                    for r in tree_rows:
                        cp.write_row(r)

            audit_trail.record(
                "graph_commit",
                conn=conn,
                object=f"mesh {src_file}",
                rows_affected=len(edges) + len(tree_rows),
                run_id=run_id,
            )
            total_mesh_hierarchy += len(edges)
            total_mesh_tree += len(tree_rows)

    return {
        "source_files": handled,
        "cites": total_cites,
        "mesh": total_mesh,
        "missing_xml": missing_xml,
        "skipped_no_pmid": skipped_no_pmid,
        "parts": total_parts,
        "mesh_hierarchy": total_mesh_hierarchy,
        "mesh_tree": total_mesh_tree,
        "mesh_empty_parse": empty_parse,
    }


_KINDS = (
    "mesh",
    "cites",
    "part",
    "mesh_parent",
    "mesh_child",
    "cited_by",
    "comments",
    "chemical",
    "keyword",
    "author",
    "facet",
)
_KINDS_MSG = (
    "unknown neighbours kind {kind!r}; expected 'mesh', 'cites', 'part', 'mesh_parent', "
    "'mesh_child', 'cited_by', 'comments', 'chemical', 'keyword', 'author' or 'facet'"
)


def neighbours(
    conn, pmid: str, hops: int = 1, kind: str = "mesh", ref_type: str | None = None
) -> list[str]:
    """Hop-bounded neighbours of ``pmid``.

    ``kind="mesh"``: sorted distinct ``COALESCE(descriptor_ui, descriptor_name)``
    for ``article_mesh`` rows of ``pmid`` -- direct tags only; ``hops`` is
    IGNORED for mesh in SP1-beta.

    ``kind="cites"``: sorted distinct ``dst_pmid`` reachable from ``pmid`` in
    ``article_cites`` within ``hops`` levels (``hops=1`` = direct citations),
    the seed excluded.

    ``kind="part"``: sorted distinct ids on the other end of an
    ``episteme.article_parts`` edge touching ``pmid`` -- its parts when ``pmid``
    is a container, its container when ``pmid`` is a part. Direct edges only;
    ``hops`` is IGNORED for part. Always answered via the recursive-CTE back
    end (never routed through SQL/PGQ).

    ``kind="mesh_parent"``/``"mesh_child"``: sorted distinct descriptor UIs one
    edge away in ``episteme.mesh_hierarchy`` (``mesh_parent`` walks up, i.e.
    toward broader terms; ``mesh_child`` walks down). Direct edges only;
    ``hops`` is IGNORED. Always answered via the recursive-CTE back end
    (never routed through SQL/PGQ), same as ``part``.

    ``kind="cited_by"``: sorted distinct ``src_pmid`` of articles citing ``pmid``
    (direct only). ``"comments"``: ``"<ref_type>:<ref_pmid or ''>"`` (optionally
    filtered by ``ref_type``). ``"chemical"``: ``substance_ui``. ``"keyword"``:
    ``lower(keyword)``. ``"author"``: ``"<position>:<name>:<orcid or ''>"``.
    ``"facet"``: ``"<facet>:<descriptor_ui>"``. All direct lookups; ``hops`` is
    IGNORED.

    Any other ``kind`` raises ``ValueError``. When the ``episteme_graph``
    property graph exists, ``cites`` is answered via SQL/PGQ; otherwise (and
    always on this build) via a recursive CTE.
    """
    if kind not in _KINDS:
        raise ValueError(_KINDS_MSG.format(kind=kind))
    if kind == "cites" and _pgq_available(conn):
        return _neighbours_pgq(conn, pmid, hops)
    return _neighbours_cte(conn, pmid, hops, kind, ref_type)


def _neighbours_cte(
    conn, pmid: str, hops: int, kind: str, ref_type: str | None = None
) -> list[str]:
    """Recursive-CTE / direct-lookup back end for :func:`neighbours`."""
    if kind == "mesh":
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT COALESCE(descriptor_ui, descriptor_name) AS d "
                "FROM episteme.article_mesh "
                "WHERE pmid = %s AND COALESCE(descriptor_ui, descriptor_name) IS NOT NULL "
                "ORDER BY d",
                (pmid,),
            )
            return [r[0] for r in cur.fetchall()]

    if kind == "cites":
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH RECURSIVE walk(dst_pmid, depth) AS (
                    SELECT dst_pmid, 1
                      FROM episteme.article_cites
                     WHERE src_pmid = %(pmid)s AND dst_pmid IS NOT NULL
                    UNION ALL
                    SELECT c.dst_pmid, w.depth + 1
                      FROM episteme.article_cites c
                      JOIN walk w ON c.src_pmid = w.dst_pmid
                     WHERE w.depth < %(hops)s AND c.dst_pmid IS NOT NULL
                )
                SELECT DISTINCT dst_pmid
                  FROM walk
                 WHERE dst_pmid <> %(pmid)s
                 ORDER BY dst_pmid
                """,
                {"pmid": pmid, "hops": hops},
            )
            return [r[0] for r in cur.fetchall()]

    if kind == "part":
        with conn.cursor() as cur:
            cur.execute(
                "SELECT part_id FROM episteme.article_parts WHERE container_id = %s "
                "UNION "
                "SELECT container_id FROM episteme.article_parts WHERE part_id = %s",
                (pmid, pmid),
            )
            return sorted({r[0] for r in cur.fetchall()})

    if kind == "mesh_parent":
        with conn.cursor() as cur:
            cur.execute(
                "SELECT parent_descriptor_ui FROM episteme.mesh_hierarchy "
                "WHERE child_descriptor_ui = %s ORDER BY parent_descriptor_ui",
                (pmid,),
            )
            return [r[0] for r in cur.fetchall()]

    if kind == "mesh_child":
        with conn.cursor() as cur:
            cur.execute(
                "SELECT child_descriptor_ui FROM episteme.mesh_hierarchy "
                "WHERE parent_descriptor_ui = %s ORDER BY child_descriptor_ui",
                (pmid,),
            )
            return [r[0] for r in cur.fetchall()]

    simple = {
        "cited_by": (
            "SELECT DISTINCT src_pmid FROM episteme.article_cites "
            "WHERE dst_pmid = %s AND src_pmid IS NOT NULL ORDER BY 1",
            (pmid,),
        ),
        "comments": (
            "SELECT DISTINCT ref_type || ':' || COALESCE(ref_pmid, '') AS v "
            "FROM episteme.article_comments WHERE pmid = %s "
            "AND ref_type IS NOT NULL AND (%s::text IS NULL OR ref_type = %s) ORDER BY v",
            (pmid, ref_type, ref_type),
        ),
        "chemical": (
            "SELECT DISTINCT substance_ui FROM episteme.article_chemicals "
            "WHERE pmid = %s AND substance_ui IS NOT NULL ORDER BY 1",
            (pmid,),
        ),
        "keyword": (
            "SELECT DISTINCT lower(keyword) AS v FROM episteme.article_keywords "
            "WHERE pmid = %s AND keyword IS NOT NULL ORDER BY v",
            (pmid,),
        ),
        "author": (
            "SELECT DISTINCT position::text || ':' || "
            "COALESCE(collective_name, trim(COALESCE(fore_name, '') || ' ' "
            "|| COALESCE(last_name, ''))) || ':' || COALESCE(orcid, '') AS v "
            "FROM episteme.article_authors WHERE pmid = %s ORDER BY v",
            (pmid,),
        ),
        "facet": (
            "SELECT DISTINCT facet || ':' || descriptor_ui AS v "
            "FROM episteme.article_facets WHERE pmid = %s ORDER BY v",
            (pmid,),
        ),
    }
    if kind in simple:
        sql, params = simple[kind]
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [r[0] for r in cur.fetchall()]

    raise ValueError(_KINDS_MSG.format(kind=kind))


def articles_for_mesh(
    conn,
    descriptor_ui: str,
    include_descendants: bool = True,
    major_only: bool = False,
    limit: int = 1000,
) -> list[str]:
    """Sorted distinct pmids tagged with ``descriptor_ui`` (and, by default, any
    descriptor beneath it in ``mesh_tree``). ``major_only`` keeps major-topic tags."""
    uis = [descriptor_ui]
    if include_descendants:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT d.descriptor_ui FROM episteme.mesh_tree t "
                "JOIN episteme.mesh_tree d ON d.tree_number = t.tree_number "
                "OR d.tree_number LIKE t.tree_number || '.%%' "
                "WHERE t.descriptor_ui = %s",
                (descriptor_ui,),
            )
            uis = sorted({descriptor_ui, *(r[0] for r in cur.fetchall())})
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT pmid FROM episteme.article_mesh "
            "WHERE descriptor_ui = ANY(%s) AND (NOT %s OR major_topic IS TRUE) "
            "ORDER BY pmid LIMIT %s",
            (uis, major_only, limit),
        )
        return [r[0] for r in cur.fetchall()]


def articles_for_facet(
    conn, facet: str, descriptor_ui: str | None = None, limit: int = 1000
) -> list[str]:
    """Sorted distinct pmids carrying ``facet`` (optionally one descriptor)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT pmid FROM episteme.article_facets "
            "WHERE facet = %s AND (%s::text IS NULL OR descriptor_ui = %s) "
            "ORDER BY pmid LIMIT %s",
            (facet, descriptor_ui, descriptor_ui, limit),
        )
        return [r[0] for r in cur.fetchall()]


def articles_by_orcid(conn, orcid: str, limit: int = 1000) -> list[str]:
    """Sorted distinct pmids with an author carrying ``orcid`` (any URL/bare form)."""
    norm = normalize_orcid(orcid)
    if norm is None:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT pmid FROM episteme.article_authors "
            "WHERE orcid = %s ORDER BY pmid LIMIT %s",
            (norm, limit),
        )
        return [r[0] for r in cur.fetchall()]


def _pgq_available(conn) -> bool:
    """True iff the ``episteme_graph`` property graph exists.

    Probe runs inside a savepoint so a parse/permission failure cannot poison
    the caller's transaction. ``relkind = 'g'`` is the property-graph relkind
    on PG19; if that comparison errors on an older catalog, fall back to a
    name-only match.
    """
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class "
                "WHERE relname = 'episteme_graph' AND relkind = 'g')"
            )
            return bool(cur.fetchone()[0])
    except psycopg.Error:
        pass
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class WHERE relname = 'episteme_graph')"
            )
            return bool(cur.fetchone()[0])
    except psycopg.Error:
        return False


def _neighbours_pgq(conn, pmid: str, hops: int) -> list[str]:
    """SQL/PGQ back end for ``cites`` -- only reachable when ``episteme_graph``
    exists (it does NOT on this PG19beta3 build, so this is code-complete but
    exercised only where a graph is present). The exact ``GRAPH_TABLE`` grammar
    on 19beta3 is unverified; on any failure raise ``RuntimeError`` so the
    caller falls back to the CTE path. The probe runs in a savepoint so the
    caller's transaction survives the failure.
    """
    query = (
        "SELECT dst FROM GRAPH_TABLE (episteme_graph "
        "MATCH (a WHERE a.pmid = %s) -[c]->" + f"{{1,{int(hops)}}}" + " (b) "
        "COLUMNS (b.pmid AS dst))"
    )
    try:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(query, (pmid,))
            return sorted({r[0] for r in cur.fetchall() if r[0] is not None})
    except psycopg.Error as exc:
        raise RuntimeError("SQL/PGQ neighbours query failed; use the CTE path") from exc


def _facets_main(argv: list[str]) -> int:
    import argparse
    from datetime import datetime, timezone

    from episteme.config import get_settings
    from episteme.data.db.connection import connection

    p = argparse.ArgumentParser(prog="graph_builder facets", description="MeSH facet maintenance")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--rebuild", action="store_true", help="recompute stale facets per file")
    mode.add_argument("--status", action="store_true", help="exit 1 when facets are stale")
    p.add_argument("--force", action="store_true", help="rebuild every file (needs --reason)")
    p.add_argument("--reason", default=None, help="why --force is used (recorded in the audit)")
    args = p.parse_args(argv)

    if args.status:
        with connection() as conn:
            st = graph_load.facet_status(conn)
        print(" ".join(f"{k}={v}" for k, v in st.items()))
        return 1 if st["stale_rows"] else 0

    run_id = get_settings().run_id or (
        f"facets-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    try:
        with connection() as conn:
            res = graph_load.rebuild_facets(
                conn, run_id=run_id, force=args.force, reason=args.reason
            )
    except graph_load.GraphLoadError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(f"done rebuilt={res['rebuilt']} skipped={res['skipped']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    from datetime import datetime, timezone

    from episteme.config import get_settings
    from episteme.data.db.connection import connection

    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "facets":
        return _facets_main(argv[1:])

    settings = get_settings()
    p = argparse.ArgumentParser(description="Build article_cites/article_mesh from raw JATS")
    p.add_argument("--source", required=True)
    p.add_argument(
        "--raw-dir",
        type=Path,
        default=None,
        help="defaults to <raw_root>/pmc/oa_comm/xml for source=pmc; unused for pubmed",
    )
    p.add_argument(
        "--processed-dir",
        type=Path,
        default=settings.processed_root,
        help="pubmed: where the extractor wrote its markers and graph files",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="pubmed: reload files whose graph hash is unchanged (needs --reason; audited)",
    )
    p.add_argument("--reason", default=None, help="why --force is used (recorded in the audit)")
    args = p.parse_args(argv)

    run_id = (
        settings.run_id or f"{args.source}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )

    holder: list = [None]  # the connection of the attempt in progress, for stage_guard

    with stage_guard(lambda: holder[0], lock_stage="graph", db_lock=True, argv=argv) as g:
        ops_status.current().update(source=args.source)  # a direct run: the source is an argument

        def _sweep() -> None:
            with connection() as sconn:
                orphans.sweep(sconn)

        # The start-up connection gets the same retry schedule as a unit.
        try:
            with_retries(_sweep, sleep=lambda secs: _SLEEP(secs))
        except Exception as exc:  # noqa: BLE001 - recorded, then exit 1
            print(f"FAIL start-up orphan sweep: {exc!r}", file=sys.stderr)
            record_failure(args.source, "graph", "startup", repr(exc), run_id=run_id)
            return g.exit_code(1)

        if args.source == "pubmed":
            try:
                res = build(
                    None,  # build_pubmed_graph opens short connections; none is held here
                    source="pubmed",
                    raw_dir=args.raw_dir,
                    run_id=run_id,
                    processed_dir=args.processed_dir,
                    force=args.force,
                    reason=args.reason,
                    holder=holder,
                )
            except graph_load.GraphLoadError as e:
                print(f"ERROR: {e}", file=sys.stderr)
                return g.exit_code(2)
            except Exception as exc:  # noqa: BLE001 - e.g. the start-up check lost the database
                print(f"FAIL pubmed: {exc}", file=sys.stderr)
                record_failure("pubmed", "graph", "pubmed", repr(exc), run_id=run_id)
                return g.exit_code(1)
            if res["files"] == 0:
                print(
                    f"WARNING: no pubmed graph files found under {args.processed_dir}",
                    file=sys.stderr,
                )
                return g.exit_code(1)
            rows = " ".join(f"{rel}={n}" for rel, n in res["rows"].items())
            print(
                f"done files={res['files']} loaded={res['loaded']} skipped={res['skipped']} "
                f"failed={len(res['failed'])} facets={res['facets']} {rows}"
            )
            if res["failed"]:
                unit = res["stopped_at"]  # build_pubmed_graph always sets it with `failed`
                record_failure("pubmed", "graph", unit, res["error"], run_id=run_id)
                print(f"FAILED source files: {', '.join(res['failed'])}", file=sys.stderr)
                print(f"stopped_at={unit}", file=sys.stderr)
                return g.exit_code(1)
            return g.exit_code(0)

        if args.force:
            print(f"NOTE: --force has no effect for source {args.source} graph", file=sys.stderr)
        raw_dir = args.raw_dir
        if raw_dir is None:
            if args.source == "pmc":
                raw_dir = settings.raw_root / "pmc" / "oa_comm" / "xml"
            else:
                print(f"ERROR: --raw-dir required for source {args.source!r}", file=sys.stderr)
                return g.exit_code(2)

        def _build_once() -> dict[str, Any]:
            """One attempt: a connection from the pool, the whole build as one transaction."""
            with connection() as conn:
                holder[0] = conn
                try:
                    with audit_trail.deferred_mirror(conn):
                        try:
                            # One transaction, no unit progress: a long statement
                            # for the stall check.
                            with ops_status.current().long_statement(f"graph build {args.source}"):
                                res = build(
                                    conn, source=args.source, raw_dir=raw_dir, run_id=run_id
                                )
                            audit_trail.commit(conn)
                            return res
                        except BaseException as exc:
                            if not isinstance(exc, Exception):
                                try:
                                    conn.cancel_safe()
                                except Exception:  # noqa: BLE001 - best effort
                                    pass
                            try:
                                audit_trail.rollback(conn)
                            except Exception:  # noqa: BLE001 - keep the original error
                                pass
                            raise
                finally:
                    holder[0] = None

        def _on_retry(attempt: int, exc: BaseException, wait: float) -> None:
            print(f"retry {args.source} ({attempt}/{len(RETRY_WAITS)}) in {wait}s: {exc!r}")

        try:
            res = with_retries(_build_once, sleep=lambda secs: _SLEEP(secs), on_retry=_on_retry)
        except Exception as exc:  # noqa: BLE001 - permanent: record, exit 1
            print(f"FAIL {args.source}: {exc}", file=sys.stderr)
            record_failure(args.source, "graph", args.source, repr(exc), run_id=run_id)
            return g.exit_code(1)
        print(
            f"done source_files={len(res['source_files'])} cites={res['cites']} "
            f"mesh={res['mesh']} missing_xml={res['missing_xml']} "
            f"skipped_no_pmid={res['skipped_no_pmid']} parts={res['parts']} "
            f"mesh_hierarchy={res['mesh_hierarchy']} mesh_tree={res['mesh_tree']} "
            f"mesh_empty_parse={res['mesh_empty_parse']}"
        )
        return g.exit_code(0)


if __name__ == "__main__":
    raise SystemExit(main())
