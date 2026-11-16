"""Precheck, null-out and REPACK of ``episteme.article_body`` duplicate text.

Migration 0014 turns ``article_body.text`` into a VIRTUAL column; the stored column is
``text_override``. Rows loaded before 0014 hold the joined text twice. This tool:

* ``precheck``  read-only, before 0014: counts rows that would read differently after it.
* ``report``    read-only: rows, override NULL share (sampled), size per partition.
* ``nullout``   sets ``text_override`` to NULL where it equals the join exactly.
* ``repack``    ``REPACK`` of one partition (PG 19), only with ``--yes-repack``.

Every batch walks the primary key (article_id, source), so no statement scans a whole
partition. ``main()`` connects as the table owner and refuses any server except 19beta3.
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
from episteme.data.article_schema import TEXT_JOIN_SQL
from episteme.data.db import connection as db_connection
from episteme.data.db.guard import ProductionDatabaseGuardError, pool_kwargs

REQUIRED_SERVER_VERSION = "19beta3"
PARTITION_RE = re.compile(r"^article_body_[a-z0-9_]+$")
SCHEMA = "episteme"

# A part is blank when NULL or only space, tab, CR, LF (the join's definition).
_ANY_PART_NONBLANK = (
    "(coalesce(btrim(title, E' \\t\\r\\n'), '') <> ''"
    " OR coalesce(btrim(abstract, E' \\t\\r\\n'), '') <> ''"
    " OR coalesce(btrim(body_text, E' \\t\\r\\n'), '') <> '')"
)


class TextOverrideError(Exception):
    """A refusal: bad partition name, wrong server, or a missing flag."""


def _log_stdout(msg: str) -> None:
    print(msg, flush=True)


def check_server_version(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("SHOW server_version")
        row = cur.fetchone()
    conn.rollback()
    version = row[0] if row else None
    if version != REQUIRED_SERVER_VERSION:
        raise TextOverrideError(
            f"server_version is {version!r}; this tool runs only on {REQUIRED_SERVER_VERSION}"
        )


def validate_partition(conn: psycopg.Connection, partition: str) -> str:
    """Return ``partition`` when it is a partition of episteme.article_body; else raise."""
    if not isinstance(partition, str) or not PARTITION_RE.fullmatch(partition):
        raise TextOverrideError(f"invalid partition name: {partition!r}")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE i.inhparent = 'episteme.article_body'::regclass "
            "AND n.nspname = %s AND c.relname = %s",
            (SCHEMA, partition),
        )
        found = cur.fetchone()
    conn.rollback()
    if not found:
        raise TextOverrideError(f"{partition!r} is not a partition of episteme.article_body")
    return partition


def list_partitions(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE i.inhparent = 'episteme.article_body'::regclass AND n.nspname = %s "
            "ORDER BY c.relname",
            (SCHEMA,),
        )
        names = [r[0] for r in cur.fetchall()]
    conn.rollback()
    return [n for n in names if PARTITION_RE.fullmatch(n)]


def _rel(partition: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(partition))


def _text_column(conn: psycopg.Connection) -> str:
    """``text_override`` once 0014 is applied, else the stored ``text`` column."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_attribute WHERE attrelid = 'episteme.article_body'::regclass "
            "AND attname = 'text_override' AND NOT attisdropped"
        )
        found = cur.fetchone()
    conn.rollback()
    return "text_override" if found else "text"


def _next_batch_sql(partition: str) -> sql.Composed:
    """Count and last key of the next N keys after (%s, %s) in primary-key order."""
    return sql.SQL(
        "SELECT count(*), (array_agg(article_id ORDER BY article_id DESC, source DESC))[1],"
        " (array_agg(source ORDER BY article_id DESC, source DESC))[1] FROM ("
        "SELECT article_id, source FROM {rel} WHERE (article_id, source) > (%s, %s)"
        " ORDER BY article_id, source LIMIT %s) k"
    ).format(rel=_rel(partition))


def _walk(
    conn: psycopg.Connection,
    partition: str,
    batch_rows: int,
    start_after: tuple[str, str] | None,
    work: Callable[[tuple[str, str], tuple[str, str]], Any],
    log: Callable[[str], None],
) -> int:
    """Call ``work(lo, hi)`` per key range (lo exclusive, hi inclusive); return rows seen."""
    lo = start_after if start_after is not None else ("", "")
    seen = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(_next_batch_sql(partition), (lo[0], lo[1], batch_rows))
            n, hi_id, hi_src = cur.fetchone()
        conn.rollback()
        if not n:
            return seen
        hi = (hi_id, hi_src)
        work(lo, hi)
        seen += n
        lo = hi
        log(f"{partition}: scanned={seen} last_key={hi[0]!r},{hi[1]!r}")


def nullout(
    conn: psycopg.Connection,
    partition: str,
    *,
    batch_rows: int,
    log: Callable[[str], None] = _log_stdout,
    start_after: tuple[str, str] | None = None,
) -> dict:
    """Set text_override NULL where it equals the join exactly. One transaction per batch."""
    validate_partition(conn, partition)
    if batch_rows < 1:
        raise TextOverrideError("batch_rows must be positive")
    update = sql.SQL(
        "UPDATE {rel} SET text_override = NULL"
        " WHERE (article_id, source) > (%s, %s) AND (article_id, source) <= (%s, %s)"
        " AND text_override IS NOT NULL AND text_override <> ''"
        " AND text_override = " + TEXT_JOIN_SQL.replace("%", "%%")
    ).format(rel=_rel(partition))
    nulled = 0

    def work(lo: tuple[str, str], hi: tuple[str, str]) -> None:
        nonlocal nulled
        with conn.cursor() as cur:
            cur.execute(update, (lo[0], lo[1], hi[0], hi[1]))
            nulled += cur.rowcount
        conn.commit()

    rows = _walk(conn, partition, batch_rows, start_after, work, log)
    log(f"{partition}: done rows={rows} nulled={nulled}")
    return {"rows": rows, "nulled": nulled}


def precheck(
    conn: psycopg.Connection,
    partition: str,
    *,
    batch_rows: int,
    log: Callable[[str], None] = _log_stdout,
) -> dict:
    """Read-only. Count rows that would read differently after 0014 (both must be 0)."""
    validate_partition(conn, partition)
    col = _text_column(conn)
    count = sql.SQL(
        "SELECT count(*) FILTER (WHERE {col} IS NULL AND " + _ANY_PART_NONBLANK + "),"
        " count(*) FILTER (WHERE {col} = '') FROM {rel}"
        " WHERE (article_id, source) > (%s, %s) AND (article_id, source) <= (%s, %s)"
    ).format(col=sql.Identifier(col), rel=_rel(partition))
    totals = {"null_with_parts": 0, "empty_string": 0}

    def work(lo: tuple[str, str], hi: tuple[str, str]) -> None:
        with conn.cursor() as cur:
            cur.execute(count, (lo[0], lo[1], hi[0], hi[1]))
            a, b = cur.fetchone()
        conn.rollback()
        totals["null_with_parts"] += a
        totals["empty_string"] += b

    rows = _walk(conn, partition, batch_rows, None, work, log)
    return {"partition": partition, "column": col, "rows": rows, **totals}


def report(conn: psycopg.Connection) -> list[dict]:
    """Per partition: rows (reltuples), override NULL share (0.1 percent sample), size.

    The NULL count is an estimate from TABLESAMPLE SYSTEM (0.1) scaled to reltuples.
    Nothing is written."""
    col = _text_column(conn)
    out: list[dict] = []
    for part in list_partitions(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.reltuples::bigint, pg_total_relation_size(c.oid) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s",
                (SCHEMA, part),
            )
            reltuples, size = cur.fetchone()
            cur.execute(
                sql.SQL(
                    "SELECT count(*), count(*) FILTER (WHERE {c} IS NULL) "
                    "FROM {rel} TABLESAMPLE SYSTEM (0.1)"
                ).format(c=sql.Identifier(col), rel=_rel(part))
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


def repack(conn: psycopg.Connection, partition: str) -> None:
    """REPACK one partition (PG 19). Autocommit; takes ACCESS EXCLUSIVE at the swap."""
    validate_partition(conn, partition)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("REPACK {rel}").format(rel=_rel(partition)))
    finally:
        conn.autocommit = False


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
    pc.add_argument("--batch-rows", type=int)
    sub.add_parser("report", help="read-only size and override estimate per partition")
    no = sub.add_parser("nullout", help="NULL text_override where it equals the join")
    no.add_argument("--partition", required=True)
    no.add_argument("--batch-rows", type=int)
    no.add_argument("--start-after", help="resume after this 'article_id,source' key")
    rp = sub.add_parser("repack", help="REPACK one partition")
    rp.add_argument("--partition", required=True)
    rp.add_argument("--yes-repack", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.action == "repack" and not args.yes_repack:
        print("error: repack needs --yes-repack", file=sys.stderr)
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
                    print(
                        f"precheck {part}: rows={r['rows']} column={r['column']} "
                        f"null_with_parts={r['null_with_parts']} "
                        f"empty_string={r['empty_string']}"
                    )
                    bad += r["null_with_parts"] + r["empty_string"]
                print("precheck: " + ("FAIL, do not apply 0014" if bad else "ok"))
                return 1 if bad else 0
            if args.action == "nullout":
                start = None
                if args.start_after:
                    a, _, s = args.start_after.rpartition(",")
                    start = (a, s)
                r = nullout(conn, args.partition, batch_rows=batch_rows, start_after=start)
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
