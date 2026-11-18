"""Shared mapping layer of the SQL lookup API (migration 0015).

The Python lookups call the complete ("internal") functions of schema
``episteme_api_internal`` with ``call`` and map the ``claim_row`` rows to a
``LookupResult`` with ``rows_to_result``.

``claim_row`` contract (see the migration):

- ``kind`` is ``claim``, ``note`` or ``missing``.
- A claim row: ``claim_group`` starts at 1, one group per claim; ``alt_rank`` 0 is the
  claim and ``alt_rank`` > 0 its alternatives (only a ``mapped`` claim has them).
- A ``note`` row holds the note in ``text``; a ``missing`` row names the source in
  ``source``. Both have NULL ``claim_group`` and ``alt_rank``.
- ``coverage.matched`` counts the claim rows with ``alt_rank`` 0.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import psycopg
from psycopg import sql

from episteme.reference.claims import Claim, Coverage, LookupResult, Method, Provenance

INTERNAL_SCHEMA = "episteme_api_internal"

# Every internal lookup function the API layer defines. ``call`` runs only these.
INTERNAL_FUNCTIONS: frozenset[str] = frozenset(
    {
        "resolve_entity",
        "substance_family",
        "approval_status",
        "safety_actions",
        "current_label",
        "label_section_text",
        "trials",
        "faers_counts",
        "literature_by_mesh",
        "graph_neighbours",
        "search_text",
        "lookup_code",
    }
)

# Column order of episteme_api_internal.article_row.
ARTICLE_COLUMNS: tuple[str, ...] = (
    "kind",
    "article_id",
    "source",
    "title",
    "year",
    "score",
    "relation",
    "release_id",
    "licence",
    "note",
)

# Column order of episteme_api_internal.claim_row.
CLAIM_COLUMNS: tuple[str, ...] = (
    "kind",
    "claim_group",
    "alt_rank",
    "text",
    "value",
    "method",
    "confidence",
    "confidence_basis",
    "source",
    "release_id",
    "source_file",
    "record_key",
    "jurisdiction",
    "licence",
)


def call(conn: psycopg.Connection, function: str, *args: object) -> list[tuple]:
    """All rows of ``episteme_api_internal.<function>(*args)``. ``function`` must be in
    ``INTERNAL_FUNCTIONS``; it is quoted as an identifier and the arguments are bound as
    parameters."""
    if function not in INTERNAL_FUNCTIONS:
        raise ValueError(f"unknown API function {function!r}")
    query = sql.SQL("SELECT * FROM {}({})").format(
        sql.Identifier(INTERNAL_SCHEMA, function),
        sql.SQL(", ").join([sql.Placeholder()] * len(args)),
    )
    with conn.cursor() as cur:
        cur.execute(query, args)
        return cur.fetchall()


def _claim(row: dict, alternatives: tuple[Claim, ...] = ()) -> Claim:
    return Claim(
        text=row["text"],
        value=row["value"],
        method=row["method"],
        provenance=Provenance(
            source=row["source"],
            release=row["release_id"],
            source_file=row["source_file"],
            record_key=row["record_key"],
            jurisdiction=row["jurisdiction"],
        ),
        confidence=float(row["confidence"]),
        confidence_basis=row["confidence_basis"],
        alternatives=alternatives,
    )


def rows_to_result(rows: Sequence[tuple]) -> LookupResult:
    """Map ``claim_row`` tuples to a ``LookupResult``. Claims keep the order of their
    first row; alternatives are sorted by ``alt_rank``. Raises ``ValueError`` for an
    unknown ``kind``, a malformed row, or an alternative whose group has no claim."""
    groups: dict[int, dict[int, dict]] = {}
    missing: list[str] = []
    notes: list[str] = []
    for raw in rows:
        if len(raw) != len(CLAIM_COLUMNS):
            raise ValueError(f"claim_row has {len(CLAIM_COLUMNS)} columns, got {len(raw)}")
        row = dict(zip(CLAIM_COLUMNS, raw, strict=True))
        kind = row["kind"]
        if kind == "note":
            notes.append(row["text"])
        elif kind == "missing":
            missing.append(row["source"])
        elif kind == "claim":
            group, rank = row["claim_group"], row["alt_rank"]
            if group is None or rank is None:
                raise ValueError("a claim row needs claim_group and alt_rank")
            ranks = groups.setdefault(group, {})
            if rank in ranks:
                raise ValueError(f"duplicate claim row: group {group}, alt_rank {rank}")
            ranks[rank] = row
        else:
            raise ValueError(f"unknown claim_row kind {kind!r}")
    claims: list[Claim] = []
    for group, ranks in groups.items():
        if 0 not in ranks:
            raise ValueError(f"claim group {group} has alternatives but no claim")
        alternatives = tuple(_claim(ranks[r]) for r in sorted(ranks) if r != 0)
        claims.append(_claim(ranks[0], alternatives))
    return LookupResult(
        claims=tuple(claims),
        coverage=Coverage(matched=len(claims), missing=tuple(missing), notes=tuple(notes)),
    )


def article_rows_to_result(
    rows: Sequence[tuple],
    *,
    describe: Callable[[dict], str],
    confidence_basis: str,
    method: Method = "fetched",
) -> LookupResult:
    """Map ``article_row`` tuples to a ``LookupResult``.

    ``kind`` is ``article``, ``note`` or ``missing``. An ``article`` row becomes one
    claim: ``describe`` gets the row as a dict (keys ``ARTICLE_COLUMNS``) and returns
    the claim text; ``value`` is the id after the first ``:`` of ``article_id`` (the
    PMID for ``pubmed:<pmid>``); ``record_key`` is that value with the prefix of the
    source id type (``pmid:`` for ``pubmed``); ``score`` is the confidence. A ``note``
    row holds the note in ``note``; a ``missing`` row names the source in ``source``.
    Order is kept. Raises ``ValueError`` for an unknown ``kind`` or a malformed row."""
    claims: list[Claim] = []
    missing: list[str] = []
    notes: list[str] = []
    for raw in rows:
        if len(raw) != len(ARTICLE_COLUMNS):
            raise ValueError(f"article_row has {len(ARTICLE_COLUMNS)} columns, got {len(raw)}")
        row = dict(zip(ARTICLE_COLUMNS, raw, strict=True))
        kind = row["kind"]
        if kind == "note":
            notes.append(row["note"])
        elif kind == "missing":
            missing.append(row["source"])
        elif kind == "article":
            _, _, local_id = row["article_id"].partition(":")
            prefix = "pmid" if row["source"] == "pubmed" else row["source"]
            claims.append(
                Claim(
                    text=describe(row),
                    value=local_id,
                    method=method,
                    provenance=Provenance(
                        source=row["source"],
                        release=row["release_id"],
                        source_file=None,
                        record_key=f"{prefix}:{local_id}",
                        jurisdiction=None,
                    ),
                    confidence=float(row["score"]),
                    confidence_basis=confidence_basis,
                )
            )
        else:
            raise ValueError(f"unknown article_row kind {kind!r}")
    return LookupResult(
        claims=tuple(claims),
        coverage=Coverage(matched=len(claims), missing=tuple(missing), notes=tuple(notes)),
    )
