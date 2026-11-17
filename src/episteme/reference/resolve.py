"""Entity resolution: a drug name or a UNII to a ``substance_key``. Read-only.

The query is the SQL function ``episteme_api_internal.resolve_entity`` (migration 0015).
Only SELECT statements run here. ``resolve_names`` (which writes) is not called.
"""

from __future__ import annotations

from dataclasses import replace

import psycopg

from episteme.reference import _api
from episteme.reference.claims import LookupResult


def resolve_entity(
    conn: psycopg.Connection,
    text: str,
    *,
    tenant: str,
    caller: str,
    id_type: str | None = None,
) -> LookupResult:
    """Resolve a drug name or UNII to a substance.

    A substance matches by its own UNII (the trimmed input in upper case; only when
    ``id_type`` is ``None`` or ``'unii'``) or by a ``substance_xref`` row: ``unii`` on
    the UNII form or ``name`` on the name key (``norm_name``, inner space collapsed)
    when ``id_type`` is ``None``, else ``id_type`` on either form.

    Ordering rule when several substances match: a substance that is itself a moiety
    (``moiety_key`` NULL or equal to its own ``substance_key``) comes first; a salt or
    ester whose moiety is another row comes after it. Ties go to the lowest
    ``substance_key``. The first row is the claim; the others are its alternatives.

    One match: method ``fetched``, confidence 1.0. n > 1 matches: method ``mapped``,
    confidence 1/n, basis "<n> substances share this name"; ``coverage.matched`` is n
    (the claim and its alternatives). No match: no claim and ``substance`` in
    ``coverage.missing``. The release is the latest ``fda_substance`` release, when one
    is recorded. ``tenant`` and ``caller`` are accepted for a stable signature; v1 does
    not use them.
    """
    # PostgreSQL text cannot hold NUL; resolve_key drops it too.
    raw = (text or "").replace("\x00", "")
    result = _api.rows_to_result(_api.call(conn, "resolve_entity", raw, id_type))
    matched = sum(1 + len(c.alternatives) for c in result.claims)
    return LookupResult(result.claims, replace(result.coverage, matched=matched))
