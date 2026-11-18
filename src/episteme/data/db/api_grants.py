"""The reads of the SQL lookup API owner role (``episteme_api_owner``).

``apply_owner_grants`` runs ``grants/0015_api_tables.sql``: USAGE on the schema
``episteme``, SELECT on exactly the relations the API functions read, the release schema
of the latest AACT release, EXECUTE on the API functions. The table owner
(``episteme_sys_admin``) may run it, no superuser. ``api_settings sync`` and the AACT
loader (``load_aact._switch``) call it. Before ``grants/0015_api_roles.sql`` created the
role it does nothing.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

OWNER_ROLE = "episteme_api_owner"
OWNER_GRANTS = Path(__file__).resolve().parent / "grants" / "0015_api_tables.sql"


def apply_owner_grants(conn: psycopg.Connection) -> bool:
    """Apply the tables grants file in one transaction (a savepoint inside an open one).
    Returns whether the owner role exists; without it nothing is granted."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT to_regrole(%s) IS NOT NULL", (OWNER_ROLE,))
        exists = bool(cur.fetchone()[0])
        if exists:
            cur.execute(OWNER_GRANTS.read_text(encoding="utf-8"))
    return exists
