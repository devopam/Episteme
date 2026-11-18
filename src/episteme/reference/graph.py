"""One-hop graph neighbours of an article. Read-only; live joins, no edge table built.

The query runs in SQL: ``episteme_api_internal.graph_neighbours`` (migration 0015).
``kind`` is one of ``KINDS``:

- ``cites`` / ``cited_by``: the citation edges (``article_cites``), ``pubmed:<pmid>``;
- ``trial``: AACT ``study_references`` rows with the article's PMID, ``nct:<nct_id>``;
- ``label``: labels whose UNII is the UNII of a substance named (exact normalised name)
  by one of the article's MeSH chemicals (``article_chemicals``);
- ``shared_mesh``: articles that share the article's major-topic MeSH descriptors,
  scored by the sum of ``1 / articles with the descriptor`` (rare descriptors first).

The article is ``pubmed:<pmid>``, ``pmid:<pmid>``, a bare PMID or any article id whose
``articles`` row has a PMID. An unknown kind or an id without a PMID gives one note.
"""

from __future__ import annotations

import psycopg

from episteme.config import get_settings
from episteme.reference import _api
from episteme.reference.claims import LookupResult, Method

KINDS: tuple[str, ...] = ("cites", "cited_by", "shared_mesh", "trial", "label")

_BASIS: dict[str, str] = {
    "cites": "citation edge in the article's reference list",
    "cited_by": "citation edge in the citing article's reference list",
    "trial": "AACT study reference with the article's PMID",
    "label": "label UNII of a substance matched by exact normalised name to a MeSH chemical "
    "of the article",
    "shared_mesh": "shares major-topic MeSH descriptors with the article; the score in the "
    "text is the sum of 1 / (articles with the descriptor) over them",
}
_METHOD: dict[str, Method] = {
    "cites": "fetched",
    "cited_by": "fetched",
    "trial": "fetched",
    "label": "mapped",
    "shared_mesh": "computed",
}


def neighbours(
    conn: psycopg.Connection,
    article_id: str,
    kind: str,
    *,
    tenant: str,
    caller: str,
    limit: int | None = None,
) -> LookupResult:
    """The one-hop neighbours of ``article_id`` by ``kind``, in the SQL function's order
    (``shared_mesh``: score descending; the others: by id). ``limit`` (None = all) caps
    the neighbours. Each claim cites the neighbour id; a ``shared_mesh`` claim states
    its score in the text and has confidence 1. ``tenant`` and ``caller`` are
    accepted for a stable signature; v1 does not use them."""
    norm_kind = (kind or "").strip().lower()
    rows = _api.call(
        conn,
        "graph_neighbours",
        article_id,
        kind,
        None if limit is None else max(limit, 0),
        get_settings().api_shared_mesh_max_articles,
    )

    # The shared_mesh score is a sum (it can exceed 1): it goes into the claim text, and
    # the claim's confidence is 1 (the shared descriptors are a fact).
    scores: dict[str, float] = {}
    if norm_kind == "shared_mesh":
        score_at = _api.ARTICLE_COLUMNS.index("score")
        kind_at = _api.ARTICLE_COLUMNS.index("kind")
        id_at = _api.ARTICLE_COLUMNS.index("article_id")
        mapped = []
        for row in rows:
            if row[kind_at] == "article":
                scores[row[id_at]] = float(row[score_at])
                row = (*row[:score_at], 1.0, *row[score_at + 1 :])
            mapped.append(row)
        rows = mapped

    def describe(row: dict) -> str:
        text = f"{row['article_id']} ({row['year'] or 'year unknown'}): {norm_kind} of {article_id}"
        if row["title"]:
            text += f"; {row['title']}"
        if row["article_id"] in scores:
            text += f"; score {scores[row['article_id']]:.6g}"
        return text

    return _api.article_rows_to_result(
        rows,
        describe=describe,
        confidence_basis=_BASIS.get(norm_kind, ""),
        method=_METHOD.get(norm_kind, "fetched"),
    )
