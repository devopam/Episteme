"""Populate the property-graph edge tables from loaded articles + raw PMC JATS.

``build`` reads ``episteme.articles`` (source ``pmc``), locates each article's
raw JATS XML under ``raw_dir``, and derives two edge sets per ``source_file``:

  * ``episteme.article_cites`` -- ``(src_pmid, dst_pmid)`` from every
    ``<pub-id pub-id-type="pmid">`` under ``<ref-list>``.
  * ``episteme.article_mesh`` -- one row per ``<kwd>`` under any ``<kwd-group>``.
    PMC OA JATS carries no MeSH descriptors (``extract_pmc`` sets
    ``articles.mesh = None``), so the keyword group is the only signal;
    ``descriptor_ui`` is left NULL for these keyword-derived (non-MeSH) rows,
    and ``descriptor_name`` carries the keyword text; ``major_topic`` /
    ``qualifiers`` are ``NULL``. ``neighbours(kind="mesh")`` falls back to
    ``descriptor_name`` when ``descriptor_ui`` is absent.

Idempotency is DELETE-by-``source_file`` + INSERT (neither table has a primary
key). ``build`` runs inside the caller's transaction and never commits --
matching ``postgres_loader``. Each processed ``source_file`` gets one
``audit_trail.record("graph_commit", ...)`` row.

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

import sys
from pathlib import Path
from typing import Any

import defusedxml.ElementTree as ET
import psycopg

from episteme import audit_trail

_CITES_INSERT = (
    "INSERT INTO episteme.article_cites (src_pmid, dst_pmid, source_file) VALUES (%s, %s, %s)"
)
_MESH_INSERT = (
    "INSERT INTO episteme.article_mesh "
    "(pmid, descriptor_ui, descriptor_name, major_topic, qualifiers, source_file) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
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


def build(conn, *, source: str, raw_dir: Path | str, run_id: str) -> dict[str, Any]:
    """Rebuild the citation + MeSH edge tables for ``source`` from raw JATS.

    One transaction; the caller commits. For every distinct ``source_file`` in
    ``episteme.articles`` (source ``pmc``, ``pmid`` not null): DELETE its rows
    from both edge tables, re-derive them from the raw JATS under ``raw_dir``,
    ``executemany`` them back, and write one ``graph_commit`` audit row.

    Returns ``{source_files, cites, mesh, missing_xml, skipped_no_pmid}``.
    """
    if source != "pmc":
        raise NotImplementedError(f"{source} graph build is SP2+")

    raw_dir = Path(raw_dir)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, pmid, pmcid, source_file FROM episteme.articles WHERE source = 'pmc'"
        )
        article_rows = cur.fetchall()

    skipped_no_pmid = 0
    by_file: dict[str, list[tuple[str, str | None]]] = {}
    for _id, pmid, pmcid, source_file in article_rows:
        if not pmid:
            skipped_no_pmid += 1
            continue
        by_file.setdefault(source_file, []).append((pmid, pmcid))

    total_cites = 0
    total_mesh = 0
    missing_xml = 0
    handled: list[str] = []

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
            cur.execute("DELETE FROM episteme.article_cites WHERE source_file = %s", (source_file,))
            cur.execute("DELETE FROM episteme.article_mesh WHERE source_file = %s", (source_file,))
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

    return {
        "source_files": handled,
        "cites": total_cites,
        "mesh": total_mesh,
        "missing_xml": missing_xml,
        "skipped_no_pmid": skipped_no_pmid,
    }


def neighbours(conn, pmid: str, hops: int = 1, kind: str = "mesh") -> list[str]:
    """Hop-bounded neighbours of ``pmid``.

    ``kind="mesh"``: sorted distinct ``COALESCE(descriptor_ui, descriptor_name)``
    for ``article_mesh`` rows of ``pmid`` -- direct tags only; ``hops`` is
    IGNORED for mesh in SP1-beta.

    ``kind="cites"``: sorted distinct ``dst_pmid`` reachable from ``pmid`` in
    ``article_cites`` within ``hops`` levels (``hops=1`` = direct citations),
    the seed excluded.

    Any other ``kind`` raises ``ValueError``. When the ``episteme_graph``
    property graph exists, ``cites`` is answered via SQL/PGQ; otherwise (and
    always on this build) via a recursive CTE.
    """
    if kind not in ("mesh", "cites"):
        raise ValueError(f"unknown neighbours kind {kind!r}; expected 'mesh' or 'cites'")
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

    raise ValueError(f"unknown neighbours kind {kind!r}; expected 'mesh' or 'cites'")


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
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class "
                "WHERE relname = 'episteme_graph')"
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
        "MATCH (a WHERE a.pmid = %s) -[c]->" + "{1,%d}" % int(hops) + " (b) "
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
        f"skipped_no_pmid={res['skipped_no_pmid']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
