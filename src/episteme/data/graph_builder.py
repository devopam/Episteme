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

For every SP2 literature source (``pmc`` plus ``pubmed``, ``apollo``,
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

import gzip
import sys
from pathlib import Path
from typing import Any

import defusedxml.ElementTree as ET
import psycopg

from episteme import audit_trail
from episteme.data.checkpoint_markers import find_input_by_key

# Sources ``build`` knows how to graph. Only ``pmc`` has raw JATS to parse for
# cites/mesh; the rest contribute ``article_parts`` (book -> part) edges only.
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


def _local(tag: str) -> str:
    """Strip any ``{namespace}`` prefix from an ElementTree tag."""
    return tag.rsplit("}", 1)[-1] if tag else tag


def _find_xml(raw_dir: Path, pmcid: str | None) -> Path | None:
    """First ``<pmcid>*.xml`` under ``raw_dir`` whose stem is ``pmcid`` or
    ``pmcid`` + a version suffix (``PMC1`` matches ``PMC1.1.xml`` but not
    ``PMC1000.1.xml``)."""
    if not pmcid:
        return None
    for cand in Path(raw_dir).rglob(f"{pmcid}*.xml"):
        stem = cand.name[:-4]  # drop ".xml"
        if stem == pmcid or stem.startswith(f"{pmcid}."):
            return cand
    return None


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
    return gzip.open(xml_path, "rb") if xml_path.suffix == ".gz" else xml_path.open("rb")


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


def build(conn, *, source: str, raw_dir: Path | str, run_id: str) -> dict[str, Any]:
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
    mesh_hierarchy, mesh_empty_parse}``.
    """
    if source not in _GRAPH_SOURCES:
        raise NotImplementedError(f"{source} graph build is SP2+")

    raw_dir = Path(raw_dir)

    skipped_no_pmid = 0
    total_cites = 0
    total_mesh = 0
    missing_xml = 0
    handled: list[str] = []

    if source == "pmc":
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, pmid, pmcid, source_file FROM episteme.articles WHERE source = 'pmc'"
            )
            article_rows = cur.fetchall()

        by_file: dict[str, list[tuple[str, str | None]]] = {}
        for _id, pmid, pmcid, source_file in article_rows:
            if not pmid:
                skipped_no_pmid += 1
                continue
            by_file.setdefault(source_file, []).append((pmid, pmcid))

        for source_file, articles in sorted(by_file.items()):
            cites_rows: list[tuple[str, str, str]] = []
            mesh_rows: list[tuple[str, None, str, None, None, str]] = []
            seen_cites: set[tuple[str, str]] = set()
            seen_mesh: set[tuple[str, str]] = set()

            for pmid, pmcid in articles:
                xml_path = _find_xml(raw_dir, pmcid)
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

            audit_trail.record(
                "graph_commit",
                conn=conn,
                object=f"mesh {src_file}",
                rows_affected=len(edges),
                run_id=run_id,
            )
            total_mesh_hierarchy += len(edges)

    return {
        "source_files": handled,
        "cites": total_cites,
        "mesh": total_mesh,
        "missing_xml": missing_xml,
        "skipped_no_pmid": skipped_no_pmid,
        "parts": total_parts,
        "mesh_hierarchy": total_mesh_hierarchy,
        "mesh_empty_parse": empty_parse,
    }


def neighbours(conn, pmid: str, hops: int = 1, kind: str = "mesh") -> list[str]:
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

    Any other ``kind`` raises ``ValueError``. When the ``episteme_graph``
    property graph exists, ``cites`` is answered via SQL/PGQ; otherwise (and
    always on this build) via a recursive CTE.
    """
    if kind not in ("mesh", "cites", "part", "mesh_parent", "mesh_child"):
        raise ValueError(
            f"unknown neighbours kind {kind!r}; expected 'mesh', 'cites', 'part', "
            "'mesh_parent' or 'mesh_child'"
        )
    if kind == "cites" and _pgq_available(conn):
        return _neighbours_pgq(conn, pmid, hops)
    return _neighbours_cte(conn, pmid, hops, kind)


def _neighbours_cte(conn, pmid: str, hops: int, kind: str) -> list[str]:
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
                     WHERE src_pmid = %(pmid)s
                    UNION ALL
                    SELECT c.dst_pmid, w.depth + 1
                      FROM episteme.article_cites c
                      JOIN walk w ON c.src_pmid = w.dst_pmid
                     WHERE w.depth < %(hops)s
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

    raise ValueError(
        f"unknown neighbours kind {kind!r}; expected 'mesh', 'cites', 'part', "
        "'mesh_parent' or 'mesh_child'"
    )


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
            return sorted({r[0] for r in cur.fetchall()})
    except psycopg.Error as exc:
        raise RuntimeError("SQL/PGQ neighbours query failed; use the CTE path") from exc


def main(argv: list[str] | None = None) -> int:
    import argparse
    from datetime import datetime, timezone

    from episteme.config import get_settings
    from episteme.data.db.connection import connection

    settings = get_settings()
    p = argparse.ArgumentParser(description="Build article_cites/article_mesh from raw JATS")
    p.add_argument("--source", required=True)
    p.add_argument(
        "--raw-dir",
        type=Path,
        default=None,
        help="defaults to <raw_root>/pmc/oa_comm/xml for source=pmc",
    )
    args = p.parse_args(argv)

    raw_dir = args.raw_dir
    if raw_dir is None:
        if args.source == "pmc":
            raw_dir = settings.raw_root / "pmc" / "oa_comm" / "xml"
        else:
            print(f"ERROR: --raw-dir required for source {args.source!r}", file=sys.stderr)
            return 2

    run_id = (
        settings.run_id or f"{args.source}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    with connection() as conn:
        res = build(conn, source=args.source, raw_dir=raw_dir, run_id=run_id)
        conn.commit()
    print(
        f"done source_files={len(res['source_files'])} cites={res['cites']} "
        f"mesh={res['mesh']} missing_xml={res['missing_xml']} "
        f"skipped_no_pmid={res['skipped_no_pmid']} parts={res['parts']} "
        f"mesh_hierarchy={res['mesh_hierarchy']} mesh_empty_parse={res['mesh_empty_parse']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
