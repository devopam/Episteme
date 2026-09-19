"""Database-mode guard: one pure check, called from dsn_from_settings().

Modes mirror MCPG_ACCESS_MODE: ``read-only`` (any DB, connections are read-only),
``restricted`` (any DB except the production one), ``unrestricted`` (no refusal).
"""

from __future__ import annotations

import logging
from typing import Any

from episteme.config import (
    DB_MODES,  # noqa: F401  (defined in config.py, the base layer; re-exported here)
)

_LOG = logging.getLogger(__name__)

_WARNED = False


class ProductionDatabaseGuardError(RuntimeError):
    """Raised, before any connection attempt, when the DB mode forbids the target."""


def check_database_allowed(settings: Any) -> None:
    global _WARNED
    if settings.db_mode != "restricted":
        return
    if settings.pg_database != settings.production_database:
        return
    msg = (
        f"Refusing to open a connection to production database "
        f"{settings.pg_database!r} (EPISTEME_DB_MODE=restricted); no connection "
        f"was attempted. To run against production deliberately, prefix the "
        f"command with EPISTEME_DB_MODE=unrestricted; to use the secondary "
        f"database instead, set EPISTEME_DB_TARGET=secondary "
        f"(or PGDATABASE=episteme_test)."
    )
    if not _WARNED:
        _WARNED = True
        _LOG.warning(msg)
    raise ProductionDatabaseGuardError(msg)


def pool_kwargs(settings: Any) -> dict[str, str]:
    """psycopg connection kwargs for the configured mode."""
    if settings.db_mode == "read-only":
        return {"options": "-c default_transaction_read_only=on"}
    return {}
