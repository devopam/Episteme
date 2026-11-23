"""One code from the current release of a reference list.

``tenant`` and ``caller`` are required, as they are on the other lookups. They
are not read from the model. A miss is an empty claim list, not an exception.
"""

from __future__ import annotations

import psycopg

from episteme.reference import _api
from episteme.reference.claims import LookupResult


def lookup_code(
    conn: psycopg.Connection,
    system: str,
    code: str,
    *,
    tenant: str,
    caller: str,
) -> LookupResult:
    """The claim runs in SQL: ``episteme_api_internal.lookup_code`` (migration 0015)."""
    if not tenant.strip() or not caller.strip():
        raise ValueError("tenant and caller are required")
    return _api.rows_to_result(_api.call(conn, "lookup_code", system, code))
