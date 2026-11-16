"""One code from the current release of a reference list.

``tenant`` and ``caller`` are required, as they are on the other lookups. They
are not read from the model. A miss is an empty claim list, not an exception.
"""

from __future__ import annotations

import psycopg

from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance

_SQL = """
SELECT release_id, version, licence_notice, display, status, source_file
FROM episteme.code_current
WHERE system_key = %s AND code = %s
"""


def lookup_code(
    conn: psycopg.Connection,
    system: str,
    code: str,
    *,
    tenant: str,
    caller: str,
) -> LookupResult:
    if not tenant.strip() or not caller.strip():
        raise ValueError("tenant and caller are required")
    with conn.cursor() as cur:
        cur.execute(_SQL, (system, code))
        row = cur.fetchone()
    if row is None:
        return LookupResult(
            claims=(),
            coverage=Coverage(matched=0, notes=(f"no current code {code!r} in {system}",)),
        )
    release_id, version, notice, display, status, source_file = row
    shown = display or code
    text = f"{system} {code} ({version or release_id}): {shown}. {notice}"
    claim = Claim(
        text=text,
        value=shown,
        method="fetched",
        provenance=Provenance(
            source=system,
            release=release_id,
            source_file=source_file,
            record_key=code,
            jurisdiction=None,
        ),
        confidence=1.0,
        confidence_basis=f"episteme.code_current; status={status or 'unset'}",
    )
    return LookupResult(claims=(claim,), coverage=Coverage(matched=1))
