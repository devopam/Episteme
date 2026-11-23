"""The reads of the SQL lookup API owner role (``episteme_api_owner``).

Two grants files. The table owner (``episteme_sys_admin``) may run both, no superuser:

- ``grants/0015_api_tables.sql``: USAGE on the schema ``episteme``, SELECT on exactly the
  relations the API functions read, EXECUTE on the API functions.
- ``grants/0015_api_aact.sql``: USAGE and SELECT on the release schema of the latest AACT
  release; older release schemas lose them.

``apply_owner_grants`` runs both files (``api_settings sync``). ``apply_aact_grants`` runs
only the AACT file. The AACT loader (``load_aact._switch``) calls it in its switch
transaction. The switch then changes no privilege of an ``episteme`` relation or of a
function, and does not wait on, or conflict with, a concurrent DDL, FAERS swap or
settings sync on them (ruling R21). Before ``grants/0015_api_roles.sql`` created the role,
both do nothing.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

OWNER_ROLE = "episteme_api_owner"
GRANTS_DIR = Path(__file__).resolve().parent / "grants"
OWNER_GRANTS = GRANTS_DIR / "0015_api_tables.sql"
AACT_GRANTS = GRANTS_DIR / "0015_api_aact.sql"


def _apply(conn: psycopg.Connection, files: tuple[Path, ...]) -> bool:
    """Run ``files`` in one transaction (a savepoint inside an open one) when the owner
    role exists. Returns whether the role exists; without it nothing is granted."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT to_regrole(%s) IS NOT NULL", (OWNER_ROLE,))
        exists = bool(cur.fetchone()[0])
        if exists:
            for path in files:
                cur.execute(path.read_text(encoding="utf-8"))
    return exists


def apply_owner_grants(conn: psycopg.Connection) -> bool:
    """All the owner grants: the tables file, then the AACT file."""
    return _apply(conn, (OWNER_GRANTS, AACT_GRANTS))


def apply_aact_grants(conn: psycopg.Connection) -> bool:
    """Only the AACT grants (the latest release schema). The AACT switch calls this."""
    return _apply(conn, (AACT_GRANTS,))
