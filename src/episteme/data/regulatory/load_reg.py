"""Load one full snapshot of a reference feed into its own ``reg_record`` partition.

    python -m episteme.data.regulatory.load_reg --source ema|openfda
        [--release ID] [--only FEED ...] [--force --reason "..."] [--raw-dir DIR]

A release is a complete snapshot. ``load_release`` creates the release partition
(``reg_record`` is LIST by dataset, then LIST by release id), ``COPY``s the rows,
checks the counts, builds the indexes, ``ANALYZE``s, and only then moves
``reg_release.is_latest`` to the new release. A feed that fails the checks leaves
the previous latest release in place and leaves no partition behind: the work runs
inside a savepoint, and the savepoint is rolled back.

Row rules (see the module tests):

* Exact duplicate rows are dropped and counted first. The row count that is checked
  against EMA's ``meta.total_records`` is the number of distinct record keys left
  after that step (a key may repeat with other content, see below).
* A dataset with an empty ``record_key`` list uses a hash of the row as its key.
* A non-exact repeat of a ``record_key`` is not dropped silently: it is counted
  (``conflicts``) and the last row seen is kept. It is not a failure.
* A row with no value for its key fields gets the key ``h:<row hash>`` and is counted
  (``missing_key``).

``source_file`` of every row is ``input_key(file, raw_dir)``; two inputs that map to
one key stop the load. Partition and index names are built with
``psycopg.sql.Identifier`` from a sanitised stem plus a short hash of the raw value,
so a release id with odd characters can neither break nor inject. The values in
``FOR VALUES IN (...)`` are ``sql.Literal`` (DDL takes no parameters).

The loader creates tables and indexes, so the CLI connects as the table owner
(``episteme_sys_admin``), as ``bulk_indexes`` does.

Config comes from ``episteme.config``; this module never reads ``os.environ``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from episteme import audit_trail
from episteme.config import get_settings
from episteme.data import checkpoint_markers
from episteme.data.checkpoint_markers import in_skipped_dir, input_key
from episteme.data.regulatory.feeds import Feed, source_feeds
from episteme.data.regulatory.normalize import (
    fits_index,
    norm_name,
    norm_product_key,
    split_names,
)
from episteme.data.regulatory.parsers import ema_total, parse
from episteme.ops.failure import record_failure

_LOG = logging.getLogger(__name__)

SCHEMA = "episteme"
SOURCES = ("ema", "openfda")
# Formats whose file states its own row total: format -> reader of that total.
EXPECTED_TOTAL: dict[str, Callable[[Path], int | None]] = {"ema_json": ema_total}
_NAME_STEM = 16
_HASH_LEN = 8
_SLASH_FORMATS = {"dmy": "%d/%m/%Y", "mdy": "%m/%d/%Y"}


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
def _stem(value: str, width: int) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")[:width] or "x"


def _short_hash(value: str, n: int) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:n]


def partition_names(dataset: str, release_id: str) -> tuple[str, str]:
    """(dataset partition, release partition) names. Only ``[a-z0-9_]``, at most 56
    bytes (an index suffix of up to 7 bytes still fits in 63). The hash makes two
    values that sanitise alike (``a-b`` and ``a_b``) get different names."""
    ds = f"reg_record_{_stem(dataset, _NAME_STEM)}_{_short_hash(dataset, 6)}"
    leaf = f"{ds}_{_stem(release_id, 12)}_{_short_hash(release_id, _HASH_LEN)}"
    return ds, leaf


def release_id_for(feed: Feed, paths: Sequence[Path]) -> str:
    """The release id from the feed's cadence and the newest input file's UTC
    modification time: ``2026-10`` (monthly), ``2026-W43`` (weekly), ``2026-10-26``
    (daily). A re-run in the same period reloads the same release."""
    newest = max(Path(p).stat().st_mtime for p in paths)
    when = datetime.fromtimestamp(newest, tz=timezone.utc)
    cadence = feed.cadence
    if cadence == "monthly":
        return when.strftime("%Y-%m")
    if cadence == "weekly":
        iso = when.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if cadence == "daily":
        return when.strftime("%Y-%m-%d")
    raise ValueError(f"feed {feed.name}: unknown cadence {cadence!r}")


def input_keys(paths: Sequence[Path], raw_dir: Path) -> list[str]:
    """``input_key`` of each path; raises when two paths share a key (they would share
    a ``source_file`` and a marker)."""
    keys = [input_key(p, raw_dir) for p in paths]
    clash = sorted({k for k in keys if keys.count(k) > 1})
    if clash:
        raise ValueError(f"inputs share an input_key: {', '.join(clash)}")
    return keys


# ---------------------------------------------------------------------------
# Row preparation (pure)
# ---------------------------------------------------------------------------
def _flatten(value: Any) -> Iterator[Any]:
    if isinstance(value, list):
        for item in value:
            yield from _flatten(item)
    elif value is not None:
        yield value


def field_values(row: dict, path: str) -> list[str]:
    """Values at a dotted path as strings. A list met on the way is walked element by
    element; a missing field, ``None`` and an empty string give nothing."""
    current: list[Any] = [row]
    for part in path.split("."):
        nxt: list[Any] = []
        for obj in current:
            for item in obj if isinstance(obj, list) else [obj]:
                if isinstance(item, dict) and part in item:
                    nxt.append(item[part])
        current = nxt
    out = []
    for v in _flatten(current):
        s = str(v).replace("\x00", "").strip()
        if s:
            out.append(s)
    return out


def parse_date(value: Any, order: str = "dmy") -> date | None:
    """A date from an ISO string (a time part is ignored), ``YYYYMMDD``, a slash date or a
    date object; ``None`` otherwise. ``order`` is the order of a slash date: ``dmy``
    (``DD/MM/YYYY``, the default) or ``mdy`` (``MM/DD/YYYY``). It comes from the feed;
    the two orders are never tried in turn, as ``03/10/2026`` would be read wrong."""
    if order not in _SLASH_FORMATS:
        raise ValueError(f"unknown date order {order!r}")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    v = value.strip()
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        pass
    for fmt in ("%Y%m%d", _SLASH_FORMATS[order]):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def _canonical(row: dict) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def _row_digest(row: dict) -> bytes:
    return hashlib.blake2b(_canonical(row).encode("utf-8"), digest_size=16).digest()


class RowPreparer:
    """Turns parsed rows into ``reg_record`` rows and keeps the duplicate state.
    One instance serves one release, whatever the number of input files."""

    def __init__(self, feed: Feed) -> None:
        self.feed = feed
        self._digests: set[bytes] = set()
        self._keys: set[str] = set()
        self.stats = {
            "rows_in": 0,
            "duplicates": 0,
            "conflicts": 0,
            "missing_key": 0,
            "bad_dates": 0,
            # Names and product keys too long to be an index key (``fits_index``): left
            # out of the array, counted. The value stays in ``payload``.
            "long_values": 0,
        }

    @property
    def distinct_keys(self) -> int:
        return len(self._keys)

    def prepare(self, row: dict, source_file: str) -> dict | None:
        """The prepared row, or ``None`` for an exact duplicate (counted)."""
        feed, stats = self.feed, self.stats
        stats["rows_in"] += 1
        digest = _row_digest(row)
        if digest in self._digests:
            stats["duplicates"] += 1
            return None
        self._digests.add(digest)
        key = self._record_key(row, digest)
        if key in self._keys:
            stats["conflicts"] += 1
        self._keys.add(key)
        names: list[str] = []
        for path in feed.substance_fields:
            for cell in field_values(row, path):
                for part in split_names(cell):
                    n = norm_name(part)
                    if not n or n in names:
                        continue
                    if not fits_index(n):
                        stats["long_values"] += 1
                        continue
                    names.append(n)
        products: list[str] = []
        for path in feed.product_fields:
            for cell in field_values(row, path):
                p = norm_product_key(cell)
                if not p or p in products:
                    continue
                if not fits_index(p):
                    stats["long_values"] += 1
                    continue
                products.append(p)
        event_date = None
        if feed.date_field:
            raw = field_values(row, feed.date_field)
            if raw:
                event_date = parse_date(raw[0], feed.date_order)
                if event_date is None:
                    stats["bad_dates"] += 1
        return {
            "record_key": key,
            "jurisdiction": feed.jurisdiction,
            "substance_names": names or None,
            "product_keys": products or None,
            "event_date": event_date,
            "payload": row,
            "source_file": source_file,
        }

    def _record_key(self, row: dict, digest: bytes) -> str:
        if not self.feed.record_key:
            return digest.hex()
        parts = []
        for path in self.feed.record_key:
            values = field_values(row, path)
            if not values:
                self.stats["missing_key"] += 1
                return "h:" + digest.hex()
            parts.append(",".join(values))
        return "|".join(parts)


def prepare_rows(feed: Feed, rows: Iterable[dict], source_file: str) -> tuple[list[dict], dict]:
    """Prepare all of ``rows`` in memory (tests and small inputs). The loader streams
    through ``RowPreparer`` instead."""
    prep = RowPreparer(feed)
    out = [r for r in (prep.prepare(row, source_file) for row in rows) if r is not None]
    return out, prep.stats


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
class _Invalid(Exception):
    """The feed failed a check: the release is not published."""


def _strip_nul(obj: Any) -> Any:
    """``obj`` with U+0000 removed from every string and key (jsonb and text refuse it).
    Done on the values, so a literal backslash followed by ``u0000`` is left alone."""
    if isinstance(obj, str):
        return obj.replace("\x00", "")
    if isinstance(obj, list):
        return [_strip_nul(v) for v in obj]
    if isinstance(obj, dict):
        return {_strip_nul(k): _strip_nul(v) for k, v in obj.items()}
    return obj


def _dumps(obj: Any) -> str:
    # Datetimes from a workbook go through str().
    return json.dumps(_strip_nul(obj), default=str, ensure_ascii=False)


_COLUMNS = (
    "dataset",
    "release_id",
    "record_key",
    "jurisdiction",
    "substance_names",
    "product_keys",
    "event_date",
    "payload",
    "source_file",
)
_COLUMN_TYPES = ("text", "text", "text", "text", "text[]", "text[]", "date", "jsonb", "text")


def latest_release(conn: psycopg.Connection, dataset: str) -> str | None:
    """The ``release_id`` that is ``is_latest`` for ``dataset``, or ``None``."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT release_id FROM {SCHEMA}.reg_release WHERE dataset = %s AND is_latest",
            (dataset,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _ident(name: str) -> sql.Identifier:
    return sql.Identifier(SCHEMA, name)


def _create_partitions(cur: psycopg.Cursor, dataset: str, release_id: str) -> tuple[str, bool]:
    ds_name, leaf = partition_names(dataset, release_id)
    cur.execute(
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {ds} PARTITION OF {parent} "
            "FOR VALUES IN ({val}) PARTITION BY LIST (release_id)"
        ).format(ds=_ident(ds_name), parent=_ident("reg_record"), val=sql.Literal(dataset))
    )
    cur.execute("SELECT to_regclass(%s)", (f"{SCHEMA}.{leaf}",))
    existed = cur.fetchone()[0] is not None
    cur.execute(sql.SQL("DROP TABLE IF EXISTS {leaf}").format(leaf=_ident(leaf)))
    cur.execute(
        sql.SQL("CREATE TABLE {leaf} PARTITION OF {ds} FOR VALUES IN ({val})").format(
            leaf=_ident(leaf), ds=_ident(ds_name), val=sql.Literal(release_id)
        )
    )
    return leaf, existed


def _create_partitions_with_timeout(
    cur: psycopg.Cursor, dataset: str, release_id: str
) -> tuple[str, bool]:
    """``_create_partitions`` under ``lock_timeout`` (``EPISTEME_REG_LOCK_TIMEOUT_S``). A
    reader that holds an open transaction on ``reg_record`` blocks the DDL; after the
    timeout the feed fails with a clear reason instead of waiting for ever. The
    session's earlier ``lock_timeout`` is put back afterwards."""
    seconds = get_settings().reg_lock_timeout_s
    cur.execute("SELECT current_setting('lock_timeout')")
    previous = cur.fetchone()[0]
    cur.execute("SELECT set_config('lock_timeout', %s, true)", (f"{seconds}s",))
    try:
        out = _create_partitions(cur, dataset, release_id)
    except psycopg.errors.LockNotAvailable as exc:
        raise _Invalid(
            f"could not lock reg_record for the partition DDL within {seconds} s "
            "(EPISTEME_REG_LOCK_TIMEOUT_S); a transaction that reads reg_record is "
            "still open"
        ) from exc
    cur.execute("SELECT set_config('lock_timeout', %s, true)", (previous,))
    return out


def _build_indexes(cur: psycopg.Cursor, leaf: str) -> None:
    table = _ident(leaf)
    for suffix, create in (
        ("uq", "CREATE UNIQUE INDEX {idx} ON {t} (record_key)"),
        ("sn", "CREATE INDEX {idx} ON {t} USING gin (substance_names)"),
        ("pk", "CREATE INDEX {idx} ON {t} USING gin (product_keys)"),
        ("ed", "CREATE INDEX {idx} ON {t} (event_date)"),
    ):
        cur.execute(sql.SQL(create).format(idx=sql.Identifier(f"{leaf}_{suffix}"), t=table))


def _copy_rows(
    cur: psycopg.Cursor,
    leaf: str,
    feed: Feed,
    release_id: str,
    prep: RowPreparer,
    files: Sequence[tuple[Path, str]],
) -> int:
    n = 0
    stmt = sql.SQL("COPY {t} ({cols}) FROM STDIN").format(
        t=_ident(leaf), cols=sql.SQL(", ").join(sql.Identifier(c) for c in _COLUMNS)
    )
    with cur.copy(stmt) as cp:
        cp.set_types(_COLUMN_TYPES)
        for path, source_file in files:
            for row in parse(feed, path):
                out = prep.prepare(row, source_file)
                if out is None:
                    continue
                cp.write_row(
                    (
                        feed.name,
                        release_id,
                        out["record_key"],
                        out["jurisdiction"],
                        out["substance_names"],
                        out["product_keys"],
                        out["event_date"],
                        Jsonb(out["payload"], dumps=_dumps),
                        out["source_file"],
                    )
                )
                n += 1
    return n


def _check_counts(feed: Feed, paths: Sequence[Path], prep: RowPreparer) -> int | None:
    """Raise ``_Invalid`` when the row counts say the feed is short or empty.
    Returns the total the file declared, when it declares one."""
    rows_after = prep.stats["rows_in"] - prep.stats["duplicates"]
    if rows_after == 0:
        raise _Invalid("the feed has no rows")
    reader = EXPECTED_TOTAL.get(feed.format)
    if reader is None or len(paths) != 1:
        return None
    total = reader(paths[0])
    if total is None:
        raise _Invalid("the feed declares no meta.total_records")
    # EMA's total counts records, and a record key can repeat with other content (the
    # non-exact repeat is counted as a conflict and the last row is kept): compare the
    # total with the distinct keys that are left after the exact duplicates went.
    keys = prep.distinct_keys
    if keys != total:
        raise _Invalid(
            f"{keys} distinct record keys after dropping exact duplicates "
            f"({rows_after} rows), meta.total_records = {total}"
        )
    return total


def load_release(
    conn: psycopg.Connection,
    feed: Feed,
    path: Path | Sequence[Path],
    release_id: str,
    *,
    run_id: str,
    raw_dir: Path | None = None,
) -> dict:
    """Load one snapshot of ``feed`` as release ``release_id``; the caller commits.

    Returns ``{"rows", "duplicates", "status", ...}``. ``status`` is ``loaded`` or
    ``failed`` (the feed failed a check: nothing was published, ``reason`` says why).
    Also in the result: ``conflicts``, ``missing_key``, ``bad_dates``, ``long_values``, ``rows_in``,
    ``release_id``, ``total_records``. A reload of an existing release replaces its
    partition. A parse error (a truncated file) raises and leaves nothing behind.
    """
    paths = [Path(path)] if isinstance(path, (str, Path)) else [Path(p) for p in path]
    if not paths:
        raise ValueError("load_release needs at least one input file")
    root = Path(raw_dir) if raw_dir is not None else paths[0].parent
    files = list(zip(paths, input_keys(paths, root), strict=True))
    prep = RowPreparer(feed)
    result: dict[str, Any] = {"release_id": release_id, "rows": 0, "status": "failed"}
    try:
        with conn.transaction():  # a savepoint when the caller is mid-transaction
            with conn.cursor() as cur:
                leaf, existed = _create_partitions_with_timeout(cur, feed.name, release_id)
                _copy_rows(cur, leaf, feed, release_id, prep, files)
                total = _check_counts(feed, paths, prep)
                if prep.stats["conflicts"]:
                    # Keep the last row of a repeated key. A fresh heap holds the rows in
                    # load order, so the larger ctid is the later row.
                    cur.execute(
                        sql.SQL(
                            "DELETE FROM {t} a USING {t} b "
                            "WHERE a.record_key = b.record_key AND a.ctid < b.ctid"
                        ).format(t=_ident(leaf))
                    )
                _build_indexes(cur, leaf)
                cur.execute(sql.SQL("ANALYZE {t}").format(t=_ident(leaf)))
                cur.execute(sql.SQL("SELECT count(*) FROM {t}").format(t=_ident(leaf)))
                rows = cur.fetchone()[0]
                if rows != prep.distinct_keys:
                    raise _Invalid(f"{rows} rows stored, {prep.distinct_keys} expected")
                cur.execute(
                    f"UPDATE {SCHEMA}.reg_release SET is_latest = false "
                    "WHERE dataset = %s AND is_latest AND release_id <> %s",
                    (feed.name, release_id),
                )
                cur.execute(
                    f"INSERT INTO {SCHEMA}.reg_release "
                    "(dataset, release_id, rows, status, is_latest) "
                    "VALUES (%s, %s, %s, 'loaded', true) "
                    "ON CONFLICT (dataset, release_id) DO UPDATE SET "
                    "loaded_at = now(), rows = EXCLUDED.rows, status = 'loaded', is_latest = true",
                    (feed.name, release_id, rows),
                )
            audit_trail.record(
                "load_replace" if existed else "load_commit",
                conn=conn,
                object=f"{feed.name} {release_id}",
                rows_affected=rows,
                run_id=run_id,
            )
    except _Invalid as exc:
        # The savepoint is rolled back: no partition, no registry change. A failed
        # first attempt is recorded; an existing release row is never overwritten.
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {SCHEMA}.reg_release (dataset, release_id, rows, status) "
                "VALUES (%s, %s, 0, 'failed') ON CONFLICT (dataset, release_id) DO NOTHING",
                (feed.name, release_id),
            )
        result.update(reason=str(exc))
    else:
        result.update(status="loaded", rows=rows, total_records=total)
    result.update(
        duplicates=prep.stats["duplicates"],
        conflicts=prep.stats["conflicts"],
        missing_key=prep.stats["missing_key"],
        bad_dates=prep.stats["bad_dates"],
        long_values=prep.stats["long_values"],
        rows_in=prep.stats["rows_in"],
    )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def feed_files(source: str, feed: Feed, raw_dir: Path) -> list[Path]:
    """The raw files of a feed. EMA: ``<raw>/ema/<feed>.json|.xlsx``. openFDA: the zips
    under ``<raw>/openfda/drug/<tag>/`` (``fda_enforcement`` reads ``drug/enforcement``;
    ``device/`` and ``food/`` enforcement zips are not drug recalls and are left out),
    in path order."""
    if source == "ema":
        ext = {"ema_json": ".json", "article57_xlsx": ".xlsx"}[feed.format]
        p = raw_dir / f"{feed.name}{ext}"
        return [p] if p.is_file() else []
    tag = feed.name.removeprefix("fda_")
    base = raw_dir / "drug" / tag
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*.zip") if p.is_file() and not in_skipped_dir(p, base))


def marker_name_for(feed: Feed, release_id: str, raw_dir: Path) -> str:
    """Name of the load marker: ``input_key`` of ``<feed>__<release stem>_<hash>``. The
    stem and hash keep a release id with ``/`` or quotes out of the marker path."""
    safe = f"{_stem(release_id, 24)}_{_short_hash(release_id, _HASH_LEN)}"
    return input_key(raw_dir / f"{feed.name}__{safe}", raw_dir)


def database_name(conn: psycopg.Connection) -> str:
    """``current_database()`` of ``conn``. The read transaction is rolled back, so the
    connection is idle again."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            return str(cur.fetchone()[0])
    finally:
        conn.rollback()


def db_marker_name(database: str, key: str) -> str:
    """The marker name of ``key`` for one database: ``<db stem>_<hash>__<key>``. A load
    into ``episteme_test`` and one into ``episteme`` never share a marker."""
    return f"{_stem(database, 24)}_{_short_hash(database, _HASH_LEN)}__{key}"


def release_loaded(conn: psycopg.Connection, dataset: str, release_id: str) -> bool:
    """Whether the database holds release ``release_id`` of ``dataset`` with status
    ``loaded``. A marker alone never proves the load: the files can outlive the rows.
    The read is rolled back; a failed read counts as "not loaded"."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT EXISTS (SELECT 1 FROM {SCHEMA}.reg_release WHERE dataset = %s "
                "AND release_id = %s AND status = 'loaded')",
                (dataset, release_id),
            )
            return bool(cur.fetchone()[0])
    except psycopg.Error:
        _LOG.warning("could not read reg_release for %s %s", dataset, release_id, exc_info=True)
        return False
    finally:
        conn.rollback()


def _fingerprint(files: Sequence[tuple[Path, str]]) -> list[list]:
    out = []
    for path, key in files:
        st = path.stat()
        out.append([key, st.st_size, st.st_mtime_ns])
    return out


def select_feeds(source: str, only: Sequence[str] | None) -> list[Feed]:
    """The configured feeds of ``source``, narrowed by ``--only``; ``ValueError`` names
    an ``--only`` entry that is not a feed of this source."""
    feeds = source_feeds(source)
    if only:
        unknown = sorted(set(only) - {f.name for f in feeds})
        if unknown:
            raise ValueError(f"no {source} feed named {unknown!r}")
        feeds = [f for f in feeds if f.name in set(only)]
    return feeds


def run_feeds(
    conn: psycopg.Connection,
    source: str,
    feeds: Sequence[Feed],
    raw_dir: Path,
    processed_root: Path,
    *,
    release: str | None,
    force: bool,
    reason: str | None,
    run_id: str,
) -> dict:
    """Load each feed in turn. One feed's failure does not stop the others. A feed with
    no input file is a failed feed (a wrong ``--raw-dir`` must not look like success).

    A feed is skipped only when its marker (for this database) matches the input files
    AND the database confirms the release (``release_loaded``). Otherwise it loads,
    whatever the marker says. After a good EMA release, its names are resolved against
    ``substance_xref`` (``resolve_release_names``) in the same transaction.
    Returns ``{"loaded", "skipped", "failed"}`` (``failed`` lists the feed names)."""
    loaded = skipped = 0
    failed: list[str] = []
    database = database_name(conn)

    def _fail(feed: Feed, why: str) -> None:
        print(f"FAIL {feed.name}: {why}")
        try:
            record_failure(source, "load", feed.name, why, run_id=run_id)
        except Exception:  # noqa: BLE001 - the failure list below still carries it
            _LOG.exception("could not record the failure of %s", feed.name)
        failed.append(feed.name)

    for feed in feeds:
        paths = feed_files(source, feed, raw_dir)
        if not paths:
            _fail(feed, f"no input files under {raw_dir}")
            continue
        release_id = "?"
        try:
            release_id = release or release_id_for(feed, paths)
            files = list(zip(paths, input_keys(paths, raw_dir), strict=True))
            fp = _fingerprint(files)
            key = marker_name_for(feed, release_id, raw_dir)
            marker = checkpoint_markers.load_success_marker_path(
                processed_root, source, db_marker_name(database, key)
            )
            if marker.is_file() and not force:
                try:
                    stored = json.loads(marker.read_text(encoding="utf-8")).get("stats", {})
                except (OSError, ValueError):
                    stored = {}
                if stored.get("files") == fp:
                    if release_loaded(conn, feed.name, release_id):
                        print(f"skip {feed.name} {release_id}")
                        skipped += 1
                        continue
                    print(
                        f"reload {feed.name} {release_id}: a marker exists, but {database} "
                        "holds no loaded release"
                    )
            try:
                with audit_trail.deferred_mirror(conn):
                    if force:
                        audit_trail.record(
                            "force_override",
                            conn=conn,
                            object=f"{feed.name} {release_id}",
                            reason=reason,
                            run_id=run_id,
                        )
                    result = load_release(
                        conn, feed, paths, release_id, run_id=run_id, raw_dir=raw_dir
                    )
                    if result["status"] == "loaded" and source == "ema":
                        result["names"] = resolve_release_names(conn, feed, release_id, run_id)
                    audit_trail.commit(conn)
            except BaseException:
                audit_trail.rollback(conn)
                raise
            if result["status"] != "loaded":
                _fail(feed, f"{release_id}: {result.get('reason')}")
                continue
            stats = {k: v for k, v in result.items() if k != "reason"}
            stats["files"] = fp
            checkpoint_markers.write_marker(
                marker,
                {
                    "source": source,
                    "source_file": key,
                    "database": database,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "stats": stats,
                },
            )
        except Exception as exc:  # noqa: BLE001 - one feed must not stop the others
            _fail(feed, f"{release_id}: {exc!r}")
            continue
        loaded += 1
        names = result.get("names")
        names_text = (
            f" names_matched={names['matched']} names_unmatched={names['unmatched']}"
            if names
            else ""
        )
        print(
            f"ok {feed.name} release={release_id} rows={result['rows']} "
            f"duplicates={result['duplicates']} conflicts={result['conflicts']} "
            f"missing_key={result['missing_key']} bad_dates={result['bad_dates']} "
            f"long_values={result.get('long_values', 0)}{names_text}"
        )
    return {"loaded": loaded, "skipped": skipped, "failed": failed}


def release_names(conn: psycopg.Connection, dataset: str, release_id: str) -> list[str]:
    """The distinct ``substance_names`` of one release (``dataset`` and ``release_id`` as
    values, so the plan reads one leaf). Read whole: the resolution that follows COPYs on
    the same connection, so no cursor can stay open."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT DISTINCT n FROM {SCHEMA}.reg_record r, unnest(r.substance_names) AS n "
            "WHERE r.dataset = %s AND r.release_id = %s",
            (dataset, release_id),
        )
        return [row[0] for row in cur.fetchall()]


def resolve_release_names(
    conn: psycopg.Connection, feed: Feed, release_id: str, run_id: str
) -> dict:
    """Resolve the names of one loaded EMA release against ``substance_xref``
    (``load_substance.resolve_names``, source ``ema``: ``ema_name`` rows), in the caller's
    transaction, so the release and its links commit together. Records the counts in a
    ``load_commit`` audit row. Returns ``{"matched", "unmatched", "skipped_long"}``. The
    import is here because ``load_substance`` imports this module."""
    from episteme.data.regulatory.load_substance import resolve_names

    counts = resolve_names(conn, release_names(conn, feed.name, release_id), "ema")
    audit_trail.record(
        "load_commit",
        conn=conn,
        object=f"substance_xref ema_name {feed.name} {release_id}",
        rows_affected=counts["matched"],
        new_value=counts,
        run_id=run_id,
    )
    return counts


def _refresh_recall_lots(
    conn: psycopg.Connection, source: str, feeds: Sequence[Feed], outcome: dict, run_id: str
) -> None:
    """After a run that holds a good ``fda_enforcement``, rebuild its ``recall_lot`` rows, so that
    a new release never reads as "no recalls". It runs last, so it sees this run's ``fda_ndc``,
    and on a skip too, so that a re-run repairs a crash before this step. A failure is a failed
    unit of the run. The import is here because ``recall_lots`` imports this module."""
    from episteme.data.regulatory.recall_lots import RECALL_DATASET, build_recall_lots

    if RECALL_DATASET in outcome["failed"] or all(f.name != RECALL_DATASET for f in feeds):
        return
    try:
        release = latest_release(conn, RECALL_DATASET)
        if release is None:
            conn.rollback()
            return
        try:
            with audit_trail.deferred_mirror(conn):
                lots = build_recall_lots(conn, release, run_id=run_id)
                audit_trail.commit(conn)
        except BaseException:
            audit_trail.rollback(conn)
            raise
        print("recall_lot " + " ".join(f"{k}={v}" for k, v in lots.items()))
    except Exception as exc:  # noqa: BLE001 - reported, and the exit code carries it
        print(f"FAIL recall_lot: {exc!r}")
        try:
            record_failure(source, "load", "recall_lot", repr(exc), run_id=run_id)
        except Exception:  # noqa: BLE001 - the failure list below still carries it
            _LOG.exception("could not record the failure of recall_lot")
        outcome["failed"].append("recall_lot")


def main(argv: list[str] | None = None) -> int:
    from episteme.data.db import connection as db_connection
    from episteme.data.db.guard import pool_kwargs
    from episteme.ops.status import current
    from episteme.ops.stop import stage_guard

    parser = argparse.ArgumentParser(
        prog="python -m episteme.data.regulatory.load_reg",
        description="Load reference-feed snapshots into episteme.reg_record.",
    )
    parser.add_argument("--source", required=True, choices=SOURCES)
    parser.add_argument("--raw-dir", type=Path, default=None, help="default: <raw root>/<source>")
    parser.add_argument("--release", default=None, help="release id (default: from the cadence)")
    parser.add_argument("--only", action="append", default=None, metavar="FEED")
    parser.add_argument("--force", action="store_true", help="reload releases with a marker")
    parser.add_argument("--reason", default=None, help="required with --force (audited)")
    args = parser.parse_args(argv)
    if args.force and not args.reason:
        print("error: --force requires --reason", file=sys.stderr)
        return 2

    settings = get_settings()
    source: str = args.source
    raw_dir = args.raw_dir or (settings.raw_root / source)
    try:
        feeds = select_feeds(source, args.only)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    external_run_id = settings.run_id
    run_id = external_run_id or f"{source}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    own_bracket = external_run_id is None
    dsn = db_connection.admin_dsn_from_settings()
    holder: list = [None]
    outcome: dict = {"loaded": 0, "skipped": 0, "failed": []}

    with stage_guard(lambda: holder[0], lock_stage="load", db_lock=True, argv=argv, dsn=dsn) as g:
        current().update(source=source)
        with psycopg.connect(dsn, autocommit=False, **pool_kwargs(settings)) as conn:
            holder[0] = conn
            try:
                if own_bracket:
                    audit_trail.record("run_start", conn=conn, object=source, run_id=run_id)
                    conn.commit()
                outcome = run_feeds(
                    conn,
                    source,
                    feeds,
                    raw_dir,
                    settings.processed_root,
                    release=args.release,
                    force=args.force,
                    reason=args.reason,
                    run_id=run_id,
                )
                _refresh_recall_lots(conn, source, feeds, outcome, run_id)
                if own_bracket:
                    failed = outcome["failed"]
                    audit_trail.record(
                        "run_end",
                        conn=conn,
                        object=source,
                        run_id=run_id,
                        reason=f"failed: {failed}"[:500] if failed else None,
                    )
                    conn.commit()
            finally:
                holder[0] = None
        print(
            f"done feeds={len(feeds)} loaded={outcome['loaded']} skipped={outcome['skipped']} "
            f"failed={len(outcome['failed'])}"
        )
        return g.exit_code(1 if outcome["failed"] else 0)


if __name__ == "__main__":
    raise SystemExit(main())
