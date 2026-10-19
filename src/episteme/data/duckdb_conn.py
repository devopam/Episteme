"""Bounded DuckDB connections and batched fetching for the serializers."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import duckdb

from episteme.config import get_settings
from episteme.ops.profile import default_batch_rows

FETCH_BATCH_ROWS = default_batch_rows()

_MEMORY_LIMIT_RE = re.compile(r"^\d+(\.\d+)?\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)$", re.IGNORECASE)


def connect(
    *, memory_limit: str | None = None, temp_dir: Path | None = None
) -> duckdb.DuckDBPyConnection:
    """In-memory connection with a memory cap and its own spill directory.

    Each connection spills to a fresh ``<spill root>/<uuid4 hex>`` subdirectory
    (the root is ``temp_dir``, else ``EPISTEME_DUCKDB_TEMP_DIR``, else
    ``<processed_root>/_ops/_duckdb_tmp``). On a slow data disk (a 5,400 rpm USB
    HDD) a large join spills there at random-I/O speed and slows every other job on
    the disk, so the env key lets a fast local disk take the spill. Spill
    file names are not unique across connections, and closing a connection
    deletes every ``duckdb_temp_*`` file in its temp directory -- with a shared
    directory, closing one connection crashed another one still spilling
    (``--workers > 1`` or two serializer processes). The subdirectory is
    deliberately NOT pre-created: DuckDB creates it on first spill and removes
    it on close because it created it.

    Insertion order is deliberately left at DuckDB's default: callers rely on it.
    """
    settings = get_settings()
    if memory_limit is None:
        from episteme.ops.profile import get_profile

        memory_limit = get_profile().duckdb_memory_limit
    limit = memory_limit.strip()
    if not _MEMORY_LIMIT_RE.match(limit):
        raise ValueError(f"invalid DuckDB memory limit: {limit!r}")
    if temp_dir is not None:
        root = temp_dir
    elif settings.duckdb_temp_dir is not None:
        root = settings.duckdb_temp_dir
    else:
        root = settings.processed_root / "_ops" / "_duckdb_tmp"
    root.mkdir(parents=True, exist_ok=True)
    spill = root / uuid.uuid4().hex
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{limit}'")  # validated above; SET takes no parameters
    con.execute("SET temp_directory = '" + str(spill).replace("'", "''") + "'")
    return con


def fetch_batches(
    con: duckdb.DuckDBPyConnection, batch_size: int = FETCH_BATCH_ROWS
) -> Iterator[list[tuple]]:
    """Yield the rows of the connection's last executed query, batch_size at a time."""
    while True:
        rows = con.fetchmany(batch_size)
        if not rows:
            return
        yield rows
