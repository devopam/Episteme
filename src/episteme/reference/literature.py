"""Literature for a substance, from MeSH indexing. Read-only; no vectors, no text scan.

The query runs in SQL: ``episteme_api_internal.literature_by_mesh`` (migration 0015;
body 0017).
A PubMed record matches when one of its MeSH descriptors (``article_mesh``) has a name
equal to a preferred name or synonym of a member of the substance's moiety family. The
descriptor name is compared with SQL ``lower()``.

``topic``, when given, is case-insensitive and is a MeSH descriptor name, a MeSH
qualifier name (``mesh_qualifier``, for example ``adverse effects``), both (for
example ``mortality``), or neither. A descriptor topic is expanded to that descriptor
and every descriptor below it in the MeSH tree (``mesh_tree``); the record must carry
one of them, or a qualifier with the topic's name. A topic that is no descriptor name
gives a note and the qualifier match only. A topic that is neither gives only a note.
When ``mesh_qualifier`` is missing or empty, every topic can be a qualifier name.

The function has a time budget (result limit family ``time_budget_ms``). When it is
spent, the function returns the records found so far and one note that starts with
``partial: time budget of <n> ms spent;`` and names the years not fully searched.

Newest first: by the publication year of the PMID's ``pubmed`` row in ``articles``
(other sources, such as a Europe PMC manuscript with its deposit year, do not count),
then by id. A year bound applies to that year; an unknown year is last and only
without a bound. Each claim cites ``pmid:<pmid>``; its release is the latest
``pubmed`` release when ``reg_release`` has one; its ``source_file`` is the smallest
file of the matched descriptor rows.
"""

from __future__ import annotations

import psycopg

from episteme.reference import _api
from episteme.reference.claims import Coverage, LookupResult

DATASETS = ("article_mesh",)


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
    topic_text = (topic or "").strip()
    limit = max(limit, 0)
    rows = _api.call(conn, "literature_by_mesh", int(substance_key), topic_text, None, None, limit)

    def describe(row: dict) -> str:
        return (
            f"PMID {row['article_id'].partition(':')[2]} ({row['year'] or 'year unknown'}): "
            f"MeSH {row['relation'] or ''}" + (f"; topic {topic_text}" if topic_text else "")
        )

    result = _api.article_rows_to_result(
        rows, describe=describe, confidence_basis="exact MeSH descriptor name match"
    )
    if limit and len(result.claims) == limit:
        notes = (*result.coverage.notes, f"limited to the {limit} newest records")
        coverage = Coverage(result.coverage.matched, result.coverage.missing, notes)
        return LookupResult(result.claims, coverage)
    return result
