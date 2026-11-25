"""Precheck, null-out and REPACK of ``episteme.article_body`` duplicate text.

Migration 0014 turns ``article_body.text`` into a VIRTUAL column; the stored column is
``text_override``. Rows loaded before 0014 hold the joined text twice. This tool:

* ``precheck``  read-only, before 0014: counts rows that would read differently after it.
* ``report``    read-only: rows, override NULL share (sampled), size per partition.
* ``nullout``   sets ``text_override`` to NULL where it equals the join exactly.
* ``repack``    ``REPACK`` of one partition (PG 19), only with ``--yes-repack``.

Every batch walks the primary key (article_id, source), so no statement scans a whole
partition. ``nullout --order physical`` walks heap block ranges instead
(``nullout_physical``). ``main()`` connects as the table owner and refuses any server
except PostgreSQL 19.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg import sql

from episteme.config import ConfigError, get_settings
from episteme.data.article_schema import _BLANK_A, _BLANK_B, _BLANK_T, TEXT_JOIN_SQL
from episteme.data.db import connection as db_connection
from episteme.data.db.guard import ProductionDatabaseGuardError, pool_kwargs

_SERVER_VERSION_RE = re.compile(r"19(\.\d+|beta\d+|rc\d+)?")
PARTITION_RE = re.compile(r"^article_body_[a-z0-9_]+$")
DEFAULT_TABLE = "episteme.article_body"
# The table is injectable for tests only; the CLI never exposes it.
TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*\.article_body$")

# A part is blank when NULL or only space, tab, CR, LF (the join's definition).
_ANY_PART_NONBLANK = f"(NOT ({_BLANK_T}) OR NOT ({_BLANK_A}) OR NOT ({_BLANK_B}))"


class TextOverrideError(Exception):
    """A refusal: bad partition name, wrong server, or a missing flag."""


def _log_stdout(msg: str) -> None:
    print(msg, flush=True)


def is_supported_server_version(version: str) -> bool:
    """True for PostgreSQL 19 (``19.x``, ``19betaN``, ``19rcN``)."""
    return bool(_SERVER_VERSION_RE.fullmatch(version or ""))


def check_server_version(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("SHOW server_version")
        row = cur.fetchone()
    conn.rollback()
    version = row[0] if row else None
    if not isinstance(version, str) or not is_supported_server_version(version):
        raise TextOverrideError(
            f"server_version is {version!r}; this tool runs only on PostgreSQL 19"
        )


def _check_table(table: str) -> tuple[str, str]:
    if not isinstance(table, str) or not TABLE_RE.fullmatch(table):
        raise TextOverrideError(f"invalid table name: {table!r}")
    schema, _, name = table.partition(".")
    return schema, name


def validate_partition(conn: psycopg.Connection, partition: str, table: str = DEFAULT_TABLE) -> str:
    """Return ``partition`` when it is a partition of episteme.article_body; else raise."""
    schema, _ = _check_table(table)
    if not isinstance(partition, str) or not PARTITION_RE.fullmatch(partition):
        raise TextOverrideError(f"invalid partition name: {partition!r}")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE i.inhparent = %s::regclass "
            "AND n.nspname = %s AND c.relname = %s",
            (table, schema, partition),
        )
        found = cur.fetchone()
    conn.rollback()
    if not found:
        raise TextOverrideError(f"{partition!r} is not a partition of {table}")
    return partition


def list_partitions(conn: psycopg.Connection, table: str = DEFAULT_TABLE) -> list[str]:
    schema, _ = _check_table(table)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE i.inhparent = %s::regclass AND n.nspname = %s "
            "ORDER BY c.relname",
            (table, schema),
        )
        names = [r[0] for r in cur.fetchall()]
    conn.rollback()
    return [n for n in names if PARTITION_RE.fullmatch(n)]


def _rel(partition: str, table: str = DEFAULT_TABLE) -> sql.Composed:
    schema, _ = _check_table(table)
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(partition))


def _text_column(conn: psycopg.Connection, table: str = DEFAULT_TABLE) -> str:
    """``text_override`` once 0014 is applied, else the stored ``text`` column."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_attribute WHERE attrelid = %s::regclass "
            "AND attname = 'text_override' AND NOT attisdropped",
            (table,),
        )
        found = cur.fetchone()
    conn.rollback()
    return "text_override" if found else "text"


def _next_batch_sql(partition: str, table: str = DEFAULT_TABLE) -> sql.Composed:
    """Count and last key of the next N keys after (%s, %s) in primary-key order."""
    return sql.SQL(
        "SELECT count(*), (array_agg(article_id ORDER BY article_id DESC, source DESC))[1],"
        " (array_agg(source ORDER BY article_id DESC, source DESC))[1] FROM ("
        "SELECT article_id, source FROM {rel} WHERE (article_id, source) > (%s, %s)"
        " ORDER BY article_id, source LIMIT %s) k"
    ).format(rel=_rel(partition, table))


def _walk(
    conn: psycopg.Connection,
    partition: str,
    batch_rows: int,
    start_after: tuple[str, str] | None,
    work: Callable[[tuple[str, str], tuple[str, str]], Any],
    log: Callable[[str], None],
    table: str = DEFAULT_TABLE,
    extra: Callable[[], str] = lambda: "",
) -> int:
    """Call ``work(lo, hi)`` per key range (lo exclusive, hi inclusive); return rows seen."""
    lo = start_after if start_after is not None else ("", "")
    seen = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(_next_batch_sql(partition, table), (lo[0], lo[1], batch_rows))
            n, hi_id, hi_src = cur.fetchone()
        conn.rollback()
        if not n:
            return seen
        hi = (hi_id, hi_src)
        work(lo, hi)
        seen += n
        lo = hi
        # last_key is exactly the --start-after format: <article_id>,<source>
        log(f"{partition}: scanned={seen}{extra()} last_key={hi[0]},{hi[1]}")


# One predicate for both nullout orders. "%" is doubled: the statements run with params.
_NULLOUT_PREDICATE = (
    "text_override IS NOT NULL AND text_override <> ''"
    " AND text_override = " + TEXT_JOIN_SQL.replace("%", "%%")
)


def nullout(
    conn: psycopg.Connection,
    partition: str,
    *,
    batch_rows: int,
    log: Callable[[str], None] = _log_stdout,
    start_after: tuple[str, str] | None = None,
    table: str = DEFAULT_TABLE,
) -> dict:
    """Set text_override NULL where it equals the join exactly. One transaction per batch."""
    validate_partition(conn, partition, table)
    if batch_rows < 1:
        raise TextOverrideError("batch_rows must be positive")
    update = sql.SQL(
        "UPDATE {rel} SET text_override = NULL"
        " WHERE (article_id, source) > (%s, %s) AND (article_id, source) <= (%s, %s)"
        " AND " + _NULLOUT_PREDICATE
    ).format(rel=_rel(partition, table))
    nulled = 0

    def work(lo: tuple[str, str], hi: tuple[str, str]) -> None:
        nonlocal nulled
        with conn.cursor() as cur:
            cur.execute(update, (lo[0], lo[1], hi[0], hi[1]))
            nulled += cur.rowcount
        conn.commit()

    rows = _walk(
        conn,
        partition,
        batch_rows,
        start_after,
        work,
        log,
        table,
        extra=lambda: f" nulled={nulled}",
    )
    log(f"{partition}: done rows={rows} nulled={nulled}")
    return {"rows": rows, "nulled": nulled}


def nullout_physical(
    conn: psycopg.Connection,
    partition: str,
    *,
    batch_blocks: int,
    start_block: int = 0,
    log: Callable[[str], None] = _log_stdout,
    table: str = DEFAULT_TABLE,
) -> dict:
    """Like ``nullout``, but walk heap block ranges (TID Range Scan) instead of keys.

    The block count is read once at the start. Each step updates ``ctid`` in
    ``[b, b + batch_blocks)`` in one transaction, so the heap is read sequentially.
    Updated rows get new versions, possibly past the starting end block; they have
    ``text_override`` NULL and never match again, so walking to the starting end block
    covers every row that existed at the start. ``rows`` in the result is the
    ``pg_class.reltuples`` estimate at the start (rows are not counted while walking).
    Resume with ``start_block`` = the ``last_block`` of the last log line."""
    validate_partition(conn, partition, table)
    if batch_blocks < 1:
        raise TextOverrideError("batch_blocks must be positive")
    if start_block < 0:
        raise TextOverrideError("start_block must not be negative")
    schema, _ = _check_table(table)
    qualified = f"{schema}.{partition}"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_relation_size(%s::regclass) / current_setting('block_size')::int,"
            " (SELECT reltuples::bigint FROM pg_class WHERE oid = %s::regclass)",
            (qualified, qualified),
        )
        end_block, reltuples = cur.fetchone()
    conn.rollback()
    update = sql.SQL(
        "UPDATE {rel} SET text_override = NULL"
        " WHERE ctid >= %s::tid AND ctid < %s::tid"
        " AND " + _NULLOUT_PREDICATE
    ).format(rel=_rel(partition, table))
    nulled = 0
    if start_block >= end_block:
        log(f"{partition}: start_block {start_block} >= end_block {end_block}: nothing to do")
    for b in range(start_block, end_block, batch_blocks):
        last = b + batch_blocks
        with conn.cursor() as cur:
            # Never fall back to a full Seq Scan per step: only a TID Range Scan is allowed.
            cur.execute("SET LOCAL enable_seqscan = off")
            cur.execute(update, (f"({b},0)", f"({last},0)"))
            nulled += cur.rowcount
        conn.commit()
        done = min(last, end_block)
        log(f"{partition}: blocks={done}/{end_block} nulled={nulled} last_block={last}")
    log(f"{partition}: done blocks={end_block} nulled={nulled}")
    return {"rows": max(reltuples or 0, 0), "nulled": nulled, "end_block": end_block}


def precheck(
    conn: psycopg.Connection,
    partition: str,
    *,
    batch_rows: int | None = None,
    log: Callable[[str], None] = _log_stdout,
    table: str = DEFAULT_TABLE,
) -> dict:
    """Read-only. Say which phase the table is in and whether 0014 may be applied.

    Before 0014 (column ``text``): count (a) ``text IS NULL`` with a non-blank part and
    (b) ``text = ''``; ``ok`` only when both are 0. After 0014 (column ``text_override``)
    (a) does not apply (NULL then means: use the join). The sentinel rows
    (``text_override = ''``) are counted for information: ``sentinel_all_blank`` is
    harmless, ``sentinel_with_parts`` is the stored NULL-text of a row with parts.
    ``ok`` is True after 0014. One sequential read per partition; ``batch_rows`` is
    accepted for call compatibility and unused."""
    validate_partition(conn, partition, table)
    col = _text_column(conn, table)
    pre = col == "text"
    conn.read_only = True
    if pre:
        expr_a = "count(*) FILTER (WHERE {col} IS NULL AND " + _ANY_PART_NONBLANK + ")"
        expr_b = "count(*) FILTER (WHERE {col} = '')"
    else:
        expr_a = "count(*) FILTER (WHERE {col} = '' AND NOT " + _ANY_PART_NONBLANK + ")"
        expr_b = "count(*) FILTER (WHERE {col} = '' AND " + _ANY_PART_NONBLANK + ")"
    # One sequential read of the partition (ACCESS SHARE only), no key batches.
    count = sql.SQL("SELECT count(*), " + expr_a + ", " + expr_b + " FROM {rel}").format(
        col=sql.Identifier(col), rel=_rel(partition, table)
    )
    try:
        with conn.cursor() as cur:
            cur.execute(count)
            rows, first, second = cur.fetchone()
    finally:
        conn.rollback()
        conn.read_only = False
    log(f"{partition}: precheck rows={rows}")
    if pre:
        return {
            "partition": partition,
            "phase": "pre-0014",
            "column": col,
            "rows": rows,
            "null_with_parts": first,
            "empty_string": second,
            "ok": first == 0 and second == 0,
        }
    return {
        "partition": partition,
        "phase": "post-0014",
        "column": col,
        "rows": rows,
        "sentinel_all_blank": first,
        "sentinel_with_parts": second,
        "ok": True,
    }


def report(conn: psycopg.Connection, table: str = DEFAULT_TABLE) -> list[dict]:
    """Per partition: rows (reltuples), override NULL share (0.1 percent sample), size.

    The NULL count is an estimate from TABLESAMPLE SYSTEM (0.1) scaled to reltuples.
    Nothing is written."""
    col = _text_column(conn, table)
    schema, _ = _check_table(table)
    out: list[dict] = []
    parts = list_partitions(conn, table)
    conn.read_only = True
    try:
        out = _report_parts(conn, parts, col, schema, table)
    finally:
        conn.rollback()
        conn.read_only = False
    return out


def _report_parts(
    conn: psycopg.Connection, parts: list[str], col: str, schema: str, table: str
) -> list[dict]:
    out: list[dict] = []
    for part in parts:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.reltuples::bigint, pg_total_relation_size(c.oid) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s",
                (schema, part),
            )
            reltuples, size = cur.fetchone()
            cur.execute(
                sql.SQL(
                    "SELECT count(*), count(*) FILTER (WHERE {c} IS NULL) "
                    "FROM {rel} TABLESAMPLE SYSTEM (0.1)"
                ).format(c=sql.Identifier(col), rel=_rel(part, table))
            )
            sampled, sampled_null = cur.fetchone()
        conn.rollback()
        est = max(reltuples, 0)
        out.append(
            {
                "partition": part,
                "rows": est,
                "override_null_est": round(sampled_null / sampled * est) if sampled else None,
                "sampled_rows": sampled,
                "total_bytes": size,
            }
        )
    return out


def repack(
    conn: psycopg.Connection,
    partition: str,
    table: str = DEFAULT_TABLE,
    *,
    lock_timeout_s: int | None = None,
) -> None:
    """REPACK one partition (PG 19). Autocommit.

    Plain REPACK holds ACCESS EXCLUSIVE on the partition for the whole rewrite, so
    readers and writers wait for its full length. A ``lock_timeout`` makes REPACK give
    up when it cannot get the lock, so that other sessions do not queue behind it."""
    validate_partition(conn, partition, table)
    if lock_timeout_s is None:
        lock_timeout_s = get_settings().repack_lock_timeout_s
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SET lock_timeout = {}").format(sql.Literal(f"{lock_timeout_s}s")))
            try:
                cur.execute(sql.SQL("REPACK {rel}").format(rel=_rel(partition, table)))
            finally:
                cur.execute("RESET lock_timeout")
    finally:
        conn.autocommit = False


def parse_start_after(value: str) -> tuple[str, str]:
    """Parse ``<article_id>,<source>``; split on the LAST comma (a source has none)."""
    a, sep, src = value.rpartition(",")
    if not sep or not a or not src:
        raise argparse.ArgumentTypeError(
            f"--start-after must be '<article_id>,<source>' (both parts not empty): {value!r}"
        )
    return a, src


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1: {value!r}")
    return n


def _non_negative_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be at least 0: {value!r}")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m episteme.data.db.text_override",
        description="Precheck, null-out and REPACK of article_body duplicate text.",
    )
    sub = p.add_subparsers(dest="action", required=True)
    pc = sub.add_parser("precheck", help="read-only; run before migration 0014")
    g = pc.add_mutually_exclusive_group(required=True)
    g.add_argument("--partition")
    g.add_argument("--all", action="store_true")
    pc.add_argument("--batch-rows", type=_positive_int)
    sub.add_parser("report", help="read-only size and override estimate per partition")
    no = sub.add_parser("nullout", help="NULL text_override where it equals the join")
    no.add_argument("--partition", required=True)
    no.add_argument("--batch-rows", type=_positive_int)
    no.add_argument(
        "--start-after", type=parse_start_after, help="resume after this 'article_id,source' key"
    )
    no.add_argument(
        "--order",
        choices=("key", "physical"),
        default="key",
        help="key: primary-key order (default); physical: heap block ranges, for big partitions",
    )
    no.add_argument("--batch-blocks", type=_positive_int, help="blocks per step (physical only)")
    no.add_argument(
        "--start-block", type=_non_negative_int, help="resume at this block (physical only)"
    )
    rp = sub.add_parser("repack", help="REPACK one partition")
    rp.add_argument("--partition", required=True)
    rp.add_argument("--yes-repack", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.action == "repack" and not args.yes_repack:
        print("error: repack needs --yes-repack", file=sys.stderr)
        return 2

    if args.action == "nullout":
        if args.order == "physical" and args.start_after is not None:
            print("error: --start-after is for --order key; use --start-block", file=sys.stderr)
            return 2
        if args.order == "key" and (args.start_block is not None or args.batch_blocks is not None):
            print("error: --start-block and --batch-blocks need --order physical", file=sys.stderr)
            return 2

    try:
        settings = get_settings()
        dsn = db_connection.admin_dsn_from_settings()
    except (ProductionDatabaseGuardError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    batch_rows = getattr(args, "batch_rows", None) or settings.text_nullout_batch_rows
    try:
        with psycopg.connect(dsn, autocommit=False, **pool_kwargs(settings)) as conn:
            check_server_version(conn)
            if args.action == "report":
                for r in report(conn):
                    print(
                        f"{r['partition']}\trows={r['rows']}\t"
                        f"override_null_est={r['override_null_est']} "
                        f"(sample of {r['sampled_rows']} rows, TABLESAMPLE SYSTEM 0.1)\t"
                        f"bytes={r['total_bytes']}"
                    )
                return 0
            if args.action == "precheck":
                parts = list_partitions(conn) if args.all else [args.partition]
                bad = 0
                for part in parts:
                    r = precheck(conn, part, batch_rows=batch_rows)
                    if r["phase"] == "pre-0014":
                        detail = (
                            f"null_with_parts={r['null_with_parts']} "
                            f"empty_string={r['empty_string']}"
                        )
                    else:
                        detail = (
                            f"sentinel_all_blank={r['sentinel_all_blank']} "
                            f"sentinel_with_parts={r['sentinel_with_parts']}"
                        )
                    print(
                        f"precheck {part}: phase={r['phase']} rows={r['rows']} "
                        f"column={r['column']} {detail} ok={r['ok']}"
                    )
                    bad += 0 if r["ok"] else 1
                print("precheck: " + ("FAIL, do not apply 0014" if bad else "ok"))
                return 1 if bad else 0
            if args.action == "nullout" and args.order == "physical":
                r = nullout_physical(
                    conn,
                    args.partition,
                    batch_blocks=args.batch_blocks or settings.text_nullout_batch_blocks,
                    start_block=args.start_block or 0,
                )
                print(f"nullout {args.partition}: rows={r['rows']} nulled={r['nulled']}")
                return 0
            if args.action == "nullout":
                r = nullout(
                    conn,
                    args.partition,
                    batch_rows=batch_rows,
                    start_after=args.start_after,
                )
                print(f"nullout {args.partition}: rows={r['rows']} nulled={r['nulled']}")
                return 0
            repack(conn, args.partition)
            print(f"repack {args.partition}: done")
            return 0
    except (TextOverrideError, ConfigError, psycopg.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
