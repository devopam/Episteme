"""Full-text GIN index builds on ``episteme.article_body``, one partition at a time.

Migration 0015 creates ``episteme_api_internal.fts_doc`` and ``search_text``; the
indexes are NOT in the migration (one transaction on 100M+ row tables). This tool is
the operator step:

* ``build --partition article_body_X [--yes]``  ``CREATE INDEX CONCURRENTLY`` of
  ``<partition>_fts_idx``. Without ``--yes`` it checks and prints the plan only.
* ``status``  per allowed partition: exists, size, index valid / INVALID / none, size.

The index expression is ``fts_expression()``, the same call that ``search_text`` uses,
so the planner can use the index (a non-pg test checks the migration text).

Allowed partitions (``FTS_PARTITIONS``): pubmed, pmc, openalex, bookshelf, dailymed,
ema_epar. The preprint source (``europepmc_preprint``) has no partition of its own; its
rows are in ``article_body_default`` with the structured sources, which is not covered.

Before a build: the partition must be in the allowlist (else exit 2), a partition of
``episteme.article_body`` (pg_inherits) with no INVALID index of that name, and the free
space on the disk of the index tablespace must be at least 2x the partition's
``pg_table_size`` (main fork + TOAST; else exit 1). The free space is read with
``shutil.disk_usage`` on the server's directory, so the tool runs on the database host.
The directory comes from the server (``data_directory`` needs superuser or
``pg_read_all_settings``) or from ``--data-dir``. ``main()`` connects as the table owner
and refuses any server except 19beta3. Before the build, other non-idle statements in
the database (pg_stat_activity, client backends) refuse it (exit 1, listed) unless
``--force``: CREATE INDEX CONCURRENTLY waits for them in its last phase. The build runs
in autocommit with ``lock_timeout`` = ``EPISTEME_FTS_LOCK_TIMEOUT_S`` (default 0 = no
timeout: a timeout in that last wait would throw the whole build away).
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from collections.abc import Callable

import psycopg
from psycopg import sql

from episteme.config import ConfigError, get_settings
from episteme.data.db import connection as db_connection
from episteme.data.db.guard import ProductionDatabaseGuardError, pool_kwargs
from episteme.data.db.text_override import (
    TextOverrideError,
    check_server_version,
)
from episteme.data.db.text_override import (
    validate_partition as _validate_partition,
)

SCHEMA = "episteme"
PARTITION_PREFIX = "article_body_"
FTS_PARTITIONS: tuple[str, ...] = (
    "article_body_pubmed",
    "article_body_pmc",
    "article_body_openalex",
    "article_body_bookshelf",
    "article_body_dailymed",
    "article_body_ema_epar",
)
# The source values of those partitions (search_text's c_covered, same order).
FTS_SOURCES: tuple[str, ...] = tuple(p.removeprefix(PARTITION_PREFIX) for p in FTS_PARTITIONS)
# Sources whose document is the full text, not only title + abstract (fts_doc).
FULL_TEXT_SOURCES: tuple[str, ...] = ("dailymed", "ema_epar")
# Free space needed, as a multiple of the partition's pg_table_size.
FREE_SPACE_FACTOR = 2


class FtsRefused(Exception):
    """A refusal before the build: partition not allowed or not found, INVALID index."""


class FtsSpaceError(Exception):
    """Not enough free space (or unknown directory) for the index build."""


def _log_stdout(msg: str) -> None:
    print(msg, flush=True)


def fts_expression(alias: str = "") -> str:
    """The indexed call; ``alias`` (for example ``"b."``) prefixes each column."""
    cols = ("source", "title", "abstract", "body_text", "text_override")
    return "episteme_api_internal.fts_doc(" + ", ".join(alias + c for c in cols) + ")"


def index_name(partition: str) -> str:
    return f"{partition}_fts_idx"


def check_allowed(partition: str) -> str:
    if partition not in FTS_PARTITIONS:
        raise FtsRefused(
            f"{partition!r} is not in the full-text allowlist: {', '.join(FTS_PARTITIONS)}"
        )
    return partition


def build_sql(partition: str) -> sql.Composed:
    check_allowed(partition)
    return sql.SQL(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS {idx} ON {rel} USING gin ({expr})"
    ).format(
        idx=sql.Identifier(index_name(partition)),
        rel=sql.Identifier(SCHEMA, partition),
        expr=sql.SQL(fts_expression()),
    )


def validate_partition(conn: psycopg.Connection, partition: str) -> str:
    try:
        return _validate_partition(conn, partition)
    except TextOverrideError as exc:
        raise FtsRefused(str(exc)) from None


def index_state(conn: psycopg.Connection, partition: str) -> str | None:
    """``'valid'``, ``'invalid'`` or None (no index of that name on the partition)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT i.indisvalid FROM pg_index i "
            "JOIN pg_class ic ON ic.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = ic.relnamespace "
            "WHERE n.nspname = %s AND ic.relname = %s "
            "AND i.indrelid = to_regclass(%s)",
            (SCHEMA, index_name(partition), f"{SCHEMA}.{partition}"),
        )
        row = cur.fetchone()
    conn.rollback()
    if row is None:
        return None
    return "valid" if row[0] else "invalid"


def relation_size(conn: psycopg.Connection, partition: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_table_size(%s::regclass)", (f"{SCHEMA}.{partition}",))
        (size,) = cur.fetchone()
    conn.rollback()
    return int(size)


def index_directory(conn: psycopg.Connection) -> str | None:
    """The directory of the tablespace a new index goes to (default_tablespace, else the
    database's), or None when the server does not show it to this role."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT CASE WHEN t.spcname IN ('pg_default', 'pg_global') "
            "            THEN current_setting('data_directory', true) "
            "            ELSE nullif(pg_tablespace_location(t.oid), '') END "
            "FROM pg_tablespace t "
            "WHERE t.oid = coalesce("
            "  (SELECT oid FROM pg_tablespace "
            "    WHERE spcname = nullif(current_setting('default_tablespace'), '')),"
            "  (SELECT dattablespace FROM pg_database WHERE datname = current_database()))"
        )
        row = cur.fetchone()
    conn.rollback()
    return row[0] if row and row[0] else None


def free_bytes(directory: str) -> int:
    return shutil.disk_usage(directory).free


def check_free_space(partition: str, *, rel_bytes: int, free_bytes: int, directory: str) -> None:
    need = FREE_SPACE_FACTOR * rel_bytes
    if free_bytes < need:
        raise FtsSpaceError(
            f"{partition}: {free_bytes} bytes free on {directory}, need at least {need} "
            f"({FREE_SPACE_FACTOR}x pg_table_size {rel_bytes}); not built"
        )


def active_statements(conn: psycopg.Connection) -> list[tuple[int, str, str]]:
    """Other non-idle client statements in this database: (pid, duration, query prefix).
    'idle in transaction' counts: CREATE INDEX CONCURRENTLY waits for it. A session the
    role may not see (state NULL) counts too."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pid, coalesce((now() - query_start)::text, '?'),"
            " left(coalesce(query, ''), 80) FROM pg_stat_activity"
            " WHERE datname = current_database() AND pid <> pg_backend_pid()"
            " AND backend_type = 'client backend' AND state IS DISTINCT FROM 'idle'"
            " ORDER BY query_start NULLS LAST, pid"
        )
        rows = [(int(r[0]), str(r[1]), str(r[2])) for r in cur.fetchall()]
    conn.rollback()
    return rows


def check_quiet(conn: psycopg.Connection, partition: str, *, force: bool, log) -> None:
    rows = active_statements(conn)
    if not rows:
        return
    listing = "; ".join(f"pid={p} duration={d} query={q!r}" for p, d, q in rows)
    if force:
        log(f"{partition}: --force: building although {len(rows)} statement(s) run: {listing}")
        return
    raise FtsRefused(
        f"{partition}: {len(rows)} other statement(s) running in this database; CREATE INDEX "
        f"CONCURRENTLY would wait for them (use --force to build anyway): {listing}"
    )


def build(
    conn: psycopg.Connection,
    partition: str,
    *,
    yes: bool,
    force: bool = False,
    free_bytes_of: Callable[[str], int] = free_bytes,
    directory: str | None = None,
    lock_timeout_s: int | None = None,
    log: Callable[[str], None] = _log_stdout,
) -> dict:
    """Check, then (with ``yes``) build ``<partition>_fts_idx`` CONCURRENTLY.

    Returns ``{"partition", "built", "state"}``. A valid index already there: nothing
    to do. An INVALID one (a failed concurrent build): refused; the message names the
    DROP INDEX CONCURRENTLY step."""
    check_allowed(partition)
    validate_partition(conn, partition)
    idx = index_name(partition)
    state = index_state(conn, partition)
    if state == "valid":
        log(f"{partition}: {idx} exists and is valid; nothing to do")
        return {"partition": partition, "built": False, "state": state}
    if state == "invalid":
        raise FtsRefused(
            f"{partition}: {idx} is INVALID (a failed concurrent build). Drop it first: "
            f"DROP INDEX CONCURRENTLY {SCHEMA}.{idx}; then run the build again"
        )
    rel_bytes = relation_size(conn, partition)
    where = directory or index_directory(conn)
    if not where:
        raise FtsSpaceError(
            f"{partition}: the server does not show its data directory to this role "
            "(needs pg_read_all_settings); pass --data-dir with the index disk's path"
        )
    free = free_bytes_of(where)
    check_free_space(partition, rel_bytes=rel_bytes, free_bytes=free, directory=where)
    stmt = build_sql(partition)
    log(f"{partition}: rel_bytes={rel_bytes} free_bytes={free} on {where}")
    if not yes:
        log(f"{partition}: plan only (add --yes to build): {stmt.as_string(conn)}")
        return {"partition": partition, "built": False, "state": None}
    check_quiet(conn, partition, force=force, log=log)
    if lock_timeout_s is None:
        lock_timeout_s = get_settings().fts_lock_timeout_s
    started = time.monotonic()
    log(f"{partition}: building {idx} (lock_timeout={lock_timeout_s}s)")
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SET lock_timeout = {}").format(sql.Literal(f"{lock_timeout_s}s")))
            try:
                cur.execute(stmt)
            except BaseException:
                # The original error wins: a failing RESET must not mask it.
                try:
                    cur.execute("RESET lock_timeout")
                except psycopg.Error:
                    pass
                raise
            cur.execute("RESET lock_timeout")
    finally:
        conn.autocommit = False
    state = index_state(conn, partition)
    log(f"{partition}: {idx} state={state} seconds={time.monotonic() - started:.0f}")
    if state != "valid":
        raise FtsRefused(f"{partition}: {idx} is {state} after the build")
    return {"partition": partition, "built": True, "state": state}


def status(conn: psycopg.Connection) -> list[dict]:
    """Per allowed partition: exists, rel_bytes, index (valid / invalid / None),
    index_bytes. Read-only."""
    out: list[dict] = []
    for part in FTS_PARTITIONS:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.oid IS NOT NULL AND EXISTS ("
                "  SELECT 1 FROM pg_inherits i WHERE i.inhrelid = c.oid"
                "  AND i.inhparent = 'episteme.article_body'::regclass),"
                " CASE WHEN c.oid IS NULL THEN NULL ELSE pg_relation_size(c.oid) END,"
                " pg_relation_size(to_regclass(%s))"
                " FROM (SELECT to_regclass(%s) AS oid) c",
                (f"{SCHEMA}.{index_name(part)}", f"{SCHEMA}.{part}"),
            )
            exists, rel_bytes, idx_bytes = cur.fetchone()
        conn.rollback()
        state = index_state(conn, part) if exists else None
        out.append(
            {
                "partition": part,
                "exists": bool(exists),
                "rel_bytes": rel_bytes,
                "index": state,
                "index_bytes": idx_bytes if state else None,
            }
        )
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m episteme.data.db.fts_index",
        description="Full-text GIN index builds on article_body, one partition at a time.",
    )
    sub = p.add_subparsers(dest="action", required=True)
    b = sub.add_parser("build", help="CREATE INDEX CONCURRENTLY on one partition")
    b.add_argument("--partition", required=True)
    b.add_argument("--yes", action="store_true", help="build; without it, check and plan only")
    b.add_argument(
        "--data-dir", help="the index disk's path for the free-space check (on the DB host)"
    )
    b.add_argument(
        "--force", action="store_true", help="build although other statements are running"
    )
    sub.add_parser("status", help="read-only index state per allowed partition")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.action == "build":
        try:
            check_allowed(args.partition)
        except FtsRefused as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    try:
        settings = get_settings()
        dsn = db_connection.admin_dsn_from_settings()
    except (ProductionDatabaseGuardError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        with psycopg.connect(dsn, autocommit=False, **pool_kwargs(settings)) as conn:
            check_server_version(conn)
            if args.action == "status":
                for r in status(conn):
                    print(
                        f"{r['partition']}\texists={r['exists']}\trel_bytes={r['rel_bytes']}\t"
                        f"index={r['index'] or 'none'}\tindex_bytes={r['index_bytes']}"
                    )
                return 0
            r = build(
                conn,
                args.partition,
                yes=args.yes,
                force=args.force,
                directory=args.data_dir,
                lock_timeout_s=settings.fts_lock_timeout_s,
            )
            print(f"build {args.partition}: built={r['built']} state={r['state']}")
            return 0
    except (FtsRefused, FtsSpaceError, TextOverrideError, ConfigError, psycopg.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
