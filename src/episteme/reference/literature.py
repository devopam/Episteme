"""Literature for a substance, from MeSH indexing. Read-only; no vectors, no text scan.

A PubMed record matches when one of its MeSH descriptors (``article_mesh``) has a name
equal to a preferred name or synonym of a member of the substance's moiety family (the
names of ``_scope.substance_scope``). The descriptor name is compared with SQL
``lower()``; the substance names are ``casefold`` forms. The two differ only outside
ASCII (for example ``ß``), where a match can be missed.

``topic``, when given, must also be on the record: a descriptor name or a qualifier name
(case-insensitive) of the same PMID, for example ``adverse effects``.

Newest first: by the publication year in ``articles`` (any source that carries the PMID),
then by PMID. ``article_mesh.descriptor_name`` has no index, so a call reads the whole
table; ``articles`` is read by its ``pmid`` index only.
"""

from __future__ import annotations

import psycopg

from episteme.reference._scope import SCHEMA, relation_exists, substance_scope
from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance

DATASETS = ("article_mesh",)

_TOPIC = """
  AND EXISTS (
    SELECT 1 FROM episteme.article_mesh t
    WHERE t.pmid = m.pmid
      AND (lower(t.descriptor_name) = %(topic)s
           OR EXISTS (SELECT 1 FROM unnest(t.qualifiers) q WHERE lower(q) = %(topic)s)))
"""

_SQL = """
WITH hits AS (
  SELECT DISTINCT m.pmid FROM episteme.article_mesh m
  WHERE lower(m.descriptor_name) = ANY(%(names)s)
  {topic}
)
SELECT h.pmid, y.year, d.descriptors, d.source_file
FROM hits h
LEFT JOIN LATERAL (
  SELECT NULLIF(max(a.year), 0) AS year FROM episteme.articles a WHERE a.pmid = h.pmid
) y ON true
CROSS JOIN LATERAL (
  SELECT array_agg(DISTINCT m.descriptor_name ORDER BY m.descriptor_name) AS descriptors,
         min(m.source_file) AS source_file
  FROM episteme.article_mesh m
  WHERE m.pmid = h.pmid AND lower(m.descriptor_name) = ANY(%(names)s)
) d
ORDER BY y.year DESC NULLS LAST, length(h.pmid) DESC, h.pmid DESC
LIMIT %(limit)s
"""


def literature(
    conn: psycopg.Connection,
    substance_key: int,
    topic: str | None,
    *,
    tenant: str,
    caller: str,
    limit: int = 20,
) -> LookupResult:
    """Up to ``limit`` PubMed records indexed with the substance as a MeSH descriptor,
    newest first. Each claim cites ``pmid:<pmid>``. ``tenant`` and ``caller`` are
    accepted for a stable signature; v1 does not use them."""
    scope = substance_scope(conn, substance_key)
    if scope is None:
        note = f"substance {substance_key} not found"
        return LookupResult((), Coverage(matched=0, missing=DATASETS, notes=(note,)))
    if not all(relation_exists(conn, f"{SCHEMA}.{t}") for t in ("article_mesh", "articles")):
        return LookupResult((), Coverage(matched=0, missing=DATASETS))
    if not scope.names or limit <= 0:
        return LookupResult((), Coverage(matched=0))
    topic_key = (topic or "").strip().lower()
    params = {"names": list(scope.names), "topic": topic_key, "limit": limit}
    with conn.cursor() as cur:
        cur.execute(_SQL.format(topic=_TOPIC if topic_key else ""), params)
        rows = cur.fetchall()
    claims = tuple(
        Claim(
            text=(
                f"PMID {pmid} ({year or 'year unknown'}): MeSH {', '.join(descriptors or [])}"
                + (f"; topic {topic.strip()}" if topic_key else "")
            ),
            value=pmid,
            method="fetched",
            provenance=Provenance(
                source="pubmed",
                release=None,
                source_file=source_file,
                record_key=f"pmid:{pmid}",
                jurisdiction=None,
            ),
            confidence=1.0,
            confidence_basis="exact MeSH descriptor name match",
        )
        for pmid, year, descriptors, source_file in rows
    )
    notes = (f"limited to the {limit} newest records",) if len(claims) == limit else ()
    return LookupResult(claims, Coverage(matched=len(claims), notes=notes))
