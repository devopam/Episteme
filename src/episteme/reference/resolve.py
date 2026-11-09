"""Entity resolution: a drug name or a UNII to a ``substance_key``. Read-only.

Only SELECT statements run here. ``resolve_names`` (which writes) is not called.
"""

from __future__ import annotations

import psycopg

from episteme.data.regulatory.load_substance import resolve_key
from episteme.reference.claims import Claim, Coverage, LookupResult, Method, Provenance

_SOURCE = "substance"

# A substance matches when its own UNII equals the upper-cased input, or when a
# substance_xref row matches: id_type 'unii' on the upper-cased input, id_type 'name'
# on resolve_key(input), or (when the caller names an id_type) that type on either form.
_SQL = """
SELECT s.substance_key,
       s.unii,
       s.preferred_name,
       (s.moiety_key IS NULL OR s.moiety_key = s.substance_key) AS is_moiety
FROM episteme.substance s
WHERE s.unii = %(unii)s
   OR s.substance_key IN (
        SELECT x.substance_key
        FROM episteme.substance_xref x
        WHERE (%(id_type)s::text IS NULL
               AND ((x.id_type = 'unii' AND x.id_value = %(unii)s)
                    OR (x.id_type = 'name' AND x.id_value = %(key)s)))
           OR (x.id_type = %(id_type)s::text
               AND x.id_value IN (%(unii)s, %(key)s))
   )
ORDER BY is_moiety DESC, s.substance_key ASC
"""


def _claim(row: tuple, method: Method, confidence: float, basis: str) -> Claim:
    key, unii, name, _is_moiety = row
    return Claim(
        text=name or (unii or str(key)),
        value=str(key),
        method=method,
        provenance=Provenance(
            source=_SOURCE,
            release=None,
            source_file=None,
            record_key=f"unii:{unii}" if unii else None,
            jurisdiction=None,
        ),
        confidence=confidence,
        confidence_basis=basis,
    )


def resolve_entity(
    conn: psycopg.Connection,
    text: str,
    *,
    tenant: str,
    caller: str,
    id_type: str | None = None,
) -> LookupResult:
    """Resolve a drug name or UNII to a substance.

    Ordering rule when several substances match: a substance that is itself a moiety
    (``moiety_key`` NULL or equal to its own ``substance_key``) comes first; a salt or
    ester whose moiety is another row comes after it. Ties go to the lowest
    ``substance_key``. The first row is the claim; the others are its alternatives.

    One match: method ``fetched``, confidence 1.0. n > 1 matches: method ``mapped``,
    confidence 1/n, basis "<n> substances share this name". ``tenant`` and ``caller``
    are accepted for a stable signature; v1 does not use them.
    """
    raw = (text or "").strip()
    key = resolve_key(raw)
    empty = LookupResult(claims=(), coverage=Coverage(matched=0, missing=(_SOURCE,)))
    if not raw or not key:
        return empty
    params = {"unii": raw.upper(), "key": key, "id_type": id_type}
    with conn.cursor() as cur:
        cur.execute(_SQL, params)
        rows = cur.fetchall()
    if not rows:
        return empty
    n = len(rows)
    if n == 1:
        claim = _claim(rows[0], "fetched", 1.0, "exact UNII or normalised name match")
    else:
        basis = f"{n} substances share this name"
        first = _claim(rows[0], "mapped", 1.0 / n, basis)
        claim = Claim(
            text=first.text,
            value=first.value,
            method="mapped",
            provenance=first.provenance,
            confidence=first.confidence,
            confidence_basis=basis,
            alternatives=tuple(_claim(r, "mapped", 1.0 / n, basis) for r in rows[1:]),
        )
    return LookupResult(claims=(claim,), coverage=Coverage(matched=n))
