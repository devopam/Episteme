"""Load one AACT (ClinicalTrials.gov) snapshot into its own schema, then switch the views.

    python -m episteme.data.aact.load_aact [--zip PATH] [--raw-dir DIR]
        [--force --reason "..."]

AACT is reference data: one full snapshot per release, looked up, never trained on (no
``articles`` rows). Design: ``docs/superpowers/specs/2026-11-03-beta-data-completion-design.md``
sections 4 and 9.

**Release.** The release id comes from the zip name, checked as a date and written
``YYYYMMDD``: the date at the start of the owner's download (``20261103_export_ctgov.zip``),
or the date of the scripted download (``flatfiles_2026-11-03.zip`` or
``flatfiles_20261103.zip``, from ``download_aact.sh``). Two zips that give one release id are
refused. The release loads into the schema
``aact_r<YYYYMMDD>``: one table per ``.txt`` member, with the columns of the member's header
row, all ``text``. ``studies`` also gets a typed ``date`` copy (``<column>_as_date``) of each
column that the dictionary types as ``date``; an empty or bad date is NULL there and is
counted per column. The schema ``aact`` holds one view per table of the latest release
(``SELECT *``), and ``aact.intervention_substance``.

**Reading.** Each member streams from the zip (never unzipped to disk) through
``csv.reader(delimiter="|", quotechar='"')`` over a UTF-8 text stream opened with
``newline=""``, so a ``|`` or a line break inside quotes stays in one field. An empty field is
NULL. NUL characters are removed and counted. A row with another width than the header stops
the table, with the member name and the line number. ``csv.field_size_limit`` is raised to
``MAX_FIELD_CHARS`` and no further: an unbalanced quote then fails the table instead of
reading a whole member into one field. The limit is process-wide (``csv`` keeps one value
for the interpreter), so other CSV readers in the same process see the raised limit too.

**Dictionary.** The file header drives the columns. Columns only in the file, columns only in
the dictionary, deprecated columns that are loaded, and dictionary tables missing from the zip
are reported. Only a missing table fails the load, and only when it is not one of
``KNOWN_ABSENT_TABLES`` (``mesh_headings``, ``mesh_terms``, ``study_searches``): that check
runs first, before any table loads. The dictionary date (``dictionary.DICTIONARY_DATE``) is
recorded with the release.

**Order of work** (``load_release``):

1. **Tables.** ``studies`` first, then the others in name order. One transaction per table:
   ``DROP TABLE IF EXISTS``, ``CREATE TABLE``, ``COPY`` in batches of at most
   ``load_batch_bytes`` of row text, the table's row in ``<schema>._load_state`` (input
   fingerprint, ``source_file`` = ``input_key`` of the zip, counts) and its audit row. A
   marker per table follows the commit (``marker_name``: the database name, the
   ``input_key`` of the zip and the table). The state row, not the marker, decides the skip:
   it commits with the rows. A re-run skips each table whose state row holds this input's
   fingerprint, and resumes at the first table not done.
2. **Indexes** (``CREATE INDEX IF NOT EXISTS``, a transaction per table): ``nct_id`` on every
   table that has it, and ``EXTRA_INDEXES``. An ``EXTRA_INDEXES`` column is free text, so its
   index is on the expression ``left(col, INDEX_PREFIX_CHARS)`` (500 characters, at most 2000
   bytes, under the btree key limit): one long value cannot stop the index. A query must
   compare ``left(col, 500)`` to use it (``WHERE left(name, 500) = left(%s, 500)``). Then
   ``ANALYZE``.
3. **Row checks.** ``studies`` must be in the zip; its rows must equal its distinct
   ``nct_id``; a table with zero rows fails unless its zip member is header-only and the table is in
   ``Settings.aact_may_be_empty_tables`` (default ``search_results``; listed as
   ``upstream_empty_tables``); in each other
   table with ``nct_id``, the share of rows whose ``nct_id`` is NULL or not in ``studies``
   must be at most ``EPISTEME_AACT_MAX_ORPHAN_SHARE``.
4. **Names.** The distinct names of ``interventions.name`` and
   ``intervention_other_names.name`` go through ``load_substance.resolve_names`` (source
   ``aact``: exact normalised match, ``aact_name`` xref rows; no fuzzy match). The release table
   ``intervention_substance (nct_id, intervention_id, substance_key, moiety_key, method)``
   holds each intervention's matched substances (``method`` ``normalised_name`` or
   ``normalised_other_name``), indexed on ``(nct_id, intervention_id)`` and analysed before the
   coverage count (a new table has no statistics; the count was planned as a nested loop).
   ``substance_key`` is the matched substance; ``moiety_key`` is its active moiety
   (``substance.moiety_key``, or the substance itself when that is NULL),
   the key that the FAERS signal tables count on. Coverage is reported, not checked.
5. **Switch,** one transaction under ``lock_timeout`` (``EPISTEME_REG_LOCK_TIMEOUT_S``): every
   view in ``aact`` is dropped and one view per table of the release is created (``SELECT``
   granted to ``episteme_app``), ``reg_release`` moves ``is_latest`` to the release
   (``dataset='aact'``, ``release_id='<YYYYMMDD>'``), and the ``_release`` state row marks the
   schema finished.

A failed check returns ``status`` ``failed``, records a ``failed`` ``reg_release`` row when the
release has none, and leaves the previous release serving. The partial release schema stays;
a re-run resumes it. Code never drops the schema of another release id (the runbook says
how). A read or database error after the tables also records the ``failed`` row, then
raises.

**Repeats.** A finished release (its ``_release`` state row) with the same input fingerprint
is ``skipped``. The same release id with another fingerprint is ``refused`` unless ``force``
with a ``reason`` (one ``force_override`` audit row). The fingerprint is the ``input_key``,
size and modification time of the zip, not its content hash (hashing 2.4 GiB on every run is
slow): a copied or touched zip reads as other input and is refused, which is safe, and
``--force --reason`` recovers. A release id older than the latest release
(``latest_release``) is ``refused`` too unless forced: it would switch the views back.

A forced reload renames the finished schema to ``aact_r<id>_prev_<UTC time>``: the views are
bound to the tables, not to the name, so the old release keeps serving until the new one
switches in. After a good switch, the ``aact_r<id>_prev_*`` schemas of this same release id
are dropped (a replaced copy of this release, not an older release) and listed in
``dropped_schemas``; after a failed switch they stay and keep serving. A forced reload of an
unfinished schema keeps it and resumes: its tables with this input's fingerprint are reused,
the others reload.

The loader creates schemas, tables and views, so the CLI connects as the owner
(``episteme_sys_admin``). Config comes from ``episteme.config``; this module never reads
``os.environ``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import re
import sys
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from episteme import audit_trail
from episteme.config import get_settings
from episteme.data import checkpoint_markers
from episteme.data.aact import dictionary as dct
from episteme.data.checkpoint_markers import in_skipped_dir, input_key
from episteme.data.regulatory.load_reg import (
    database_name,
    db_marker_name,
    latest_release,
    release_loaded,
)
from episteme.data.regulatory.load_substance import resolve_key, resolve_names
from episteme.ops.failure import record_failure

_LOG = logging.getLogger(__name__)

SOURCE = "aact"
DATASET = "aact"
VIEW_SCHEMA = "aact"
SCHEMA_PREFIX = "aact_r"
REG_SCHEMA = "episteme"
APP_ROLE = "episteme_app"
STATE_TABLE = "_load_state"
RELEASE_ROW = "_release"
STUDIES = "studies"
INTERVENTION_SUBSTANCE = "intervention_substance"
# Dictionary tables that the AACT zip leaves out (checked on the 2026-11-03 release; NLM
# MeSH covers the first two). Absent from the zip is allowed for these three only: any other
# dictionary table missing from the zip fails the load before a table loads, so the gate's
# tables_with_rows floor (the dictionary's 52 tables less these three: 49) holds.
KNOWN_ABSENT_TABLES: tuple[str, ...] = ("mesh_headings", "mesh_terms", "study_searches")
# A table whose zip member holds the header line only is empty upstream, not a failed load.
# The 2026-11-03 file has `search_results.txt` as the header line only (62 bytes). The row
# check fails a table only when its member has data rows and the loaded table has none.
# A change of the reading or typing rules changes the fingerprint, so a finished release is
# not taken as current after such a change.
LOADER_VERSION = 1
TYPED_DATE_SUFFIX = "_as_date"
TYPED_TABLES = (STUDIES,)
# Largest field the CSV reader accepts, in characters. The longest AACT free text
# (detailed descriptions) is far below it; an unbalanced quote fails the table instead of
# swallowing the member. Checked by the first sample run.
MAX_FIELD_CHARS = 16 * 1024 * 1024
# Indexes beyond nct_id (spec 4.2), table -> columns.
EXTRA_INDEXES: dict[str, tuple[str, ...]] = {
    "reported_events": ("adverse_event_term",),
    "interventions": ("name",),
    "conditions": ("downcase_name",),
    "browse_conditions": ("mesh_term",),
    "browse_interventions": ("mesh_term",),
    INTERVENTION_SUBSTANCE: ("substance_key", "moiety_key"),
}
# The text columns of EXTRA_INDEXES are indexed on left(col, INDEX_PREFIX_CHARS): at most
# 4 bytes a character, so 2000 bytes, under the btree key limit (about 2700 bytes).
INDEX_PREFIX_CHARS = 500
# Columns that hold no free text: indexed whole.
_WHOLE_INDEX_COLUMNS = frozenset({"nct_id", "substance_key", "moiety_key"})
# Estimated bytes of one row beyond its text, and of one field (the Python str object).
_ROW_OVERHEAD = 64
_FIELD_OVERHEAD = 50
_NAME_RE = re.compile(r"[a-z_][a-z0-9_]*\Z")
# <YYYYMMDD>_*.zip (owner download) or flatfiles_<YYYY-MM-DD|YYYYMMDD>.zip (download_aact.sh).
_RELEASE_RES = (
    re.compile(r"(\d{4})(\d{2})(\d{2})_.*\.zip\Z", re.IGNORECASE),
    re.compile(r"(\d{4})(\d{2})(\d{2})\.zip\Z", re.IGNORECASE),
    re.compile(r"flatfiles_(\d{4})-(\d{2})-(\d{2})\.zip\Z", re.IGNORECASE),
    re.compile(r"flatfiles_(\d{4})(\d{2})(\d{2})\.zip\Z", re.IGNORECASE),
)
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_MAX_IDENT = 63


class _Invalid(Exception):
    """The release failed a check: it is not published."""


# ---------------------------------------------------------------------------
# Names (pure)
# ---------------------------------------------------------------------------
def release_id_from_zip(path: Path) -> str:
    """The release id (``YYYYMMDD``) of a zip name, checked as a date:
    ``<YYYYMMDD>_*.zip`` (the owner's download, ``20261103_export_ctgov.zip``) or
    ``flatfiles_<YYYY-MM-DD>.zip`` / ``flatfiles_<YYYYMMDD>.zip`` (``download_aact.sh``).
    ``ValueError`` for another name or a date that does not exist."""
    name = Path(path).name
    for pattern in _RELEASE_RES:
        m = pattern.match(name)
        if m:
            release_id = "".join(m.groups())
            try:
                datetime.strptime(release_id, "%Y%m%d")
            except ValueError as exc:
                raise ValueError(f"{name}: {release_id} is not a date") from exc
            return release_id
    raise ValueError(
        f"{name}: not an AACT release zip (<YYYYMMDD>_*.zip or flatfiles_<YYYY-MM-DD>.zip)"
    )


def is_release_name(name: str) -> bool:
    """Whether ``name`` has the form of a release zip (the date is not checked)."""
    return any(p.match(name) for p in _RELEASE_RES)


def schema_name(release_id: str) -> str:
    """``aact_r<YYYYMMDD>``; ``ValueError`` for another release id."""
    if not re.fullmatch(r"\d{8}", release_id):
        raise ValueError(f"release id {release_id!r} is not YYYYMMDD")
    return f"{SCHEMA_PREFIX}{release_id}"


def _short_hash(value: str, n: int = 8) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:n]


def index_name(table: str, column: str) -> str:
    """``<table>_<column>_idx``; a name over 63 bytes is cut and gets a hash, so two
    indexes of one schema never share a name."""
    name = f"{table}_{column}_idx"
    if len(name.encode("utf-8")) <= _MAX_IDENT:
        return name
    h = _short_hash(f"{table}.{column}")
    return f"{name[: _MAX_IDENT - len(h) - 5]}_{h}_idx"


def marker_name(database: str, zip_path: Path, raw_dir: Path, table: str) -> str:
    """The load marker name of one table: the database name, the ``input_key`` of the zip
    and the table (``load_reg.db_marker_name``). A load into ``episteme_test`` and one into
    ``episteme`` never share a marker."""
    return db_marker_name(database, f"{input_key(zip_path, raw_dir)}__{table}")


def fingerprint(zip_path: Path, key: str) -> str:
    """SHA-256 of ``LOADER_VERSION`` and the ``input_key``, size and modification time of
    the zip."""
    st = Path(zip_path).stat()
    payload = json.dumps([LOADER_VERSION, key, st.st_size, st.st_mtime_ns])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def release_zips(raw_dir: Path) -> list[Path]:
    """The release zips under ``raw_dir`` (``release_id_from_zip`` names, a valid date), in
    release order, then path order. Other zips (``postgres_*``) are not releases."""
    base = Path(raw_dir)
    if not base.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for p in base.rglob("*.zip"):
        if not p.is_file() or in_skipped_dir(p, base) or not is_release_name(p.name):
            continue
        try:
            out.append((release_id_from_zip(p), p))
        except ValueError:
            _LOG.warning("%s: not a release zip (bad date)", p)
    return [p for _, p in sorted(out, key=lambda t: (t[0], str(t[1])))]


def find_release_zip(raw_dir: Path, zips: Sequence[Path]) -> Path:
    """The zip of the newest release among ``zips``. ``FileNotFoundError`` when there is
    none; ``ValueError`` when two zips give one release id (whichever release)."""
    if not zips:
        raise FileNotFoundError(
            f"no AACT release zip (<YYYYMMDD>_*.zip or flatfiles_<YYYY-MM-DD>.zip) under {raw_dir}"
        )
    by_id: dict[str, list[Path]] = {}
    for p in zips:
        by_id.setdefault(release_id_from_zip(p), []).append(Path(p))
    for release_id, paths in sorted(by_id.items()):
        if len(paths) > 1:
            keys = sorted(input_key(p, raw_dir) for p in paths)
            raise ValueError(
                f"release {release_id} has {len(keys)} zips: {', '.join(keys)}; they would load "
                "as one release. Keep one of them (or pass --zip PATH)"
            )
    newest = max(by_id)
    return by_id[newest][0]


# ---------------------------------------------------------------------------
# Reading (pure)
# ---------------------------------------------------------------------------
def table_members(zip_path: Path) -> dict[str, str]:
    """``{table: member}`` for the ``.txt`` members of the zip. ``ValueError`` for a
    member whose name is no plain table name, and for two members of one table."""
    out: dict[str, str] = {}
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".txt"):
                continue
            table = PurePosixPath(info.filename).stem
            _check_name(table, f"{info.filename}: table name")
            if table in out:
                raise ValueError(f"two members for table {table}: {out[table]}, {info.filename}")
            out[table] = info.filename
    return out


def member_order(tables: Sequence[str]) -> list[str]:
    """``studies`` first (the other tables' checks need it), then the others by name."""
    return sorted(tables, key=lambda t: (t != STUDIES, t))


def _iter_rows(zip_path: Path, member: str) -> Iterator[tuple[int, list[str]]]:
    """``(line number, fields)`` of each record of ``member``, header first."""
    csv.field_size_limit(max(csv.field_size_limit(), MAX_FIELD_CHARS))
    with zipfile.ZipFile(zip_path) as zf, zf.open(member) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        reader = csv.reader(text, delimiter="|", quotechar='"')
        try:
            for row in reader:
                yield reader.line_num, row
        except csv.Error as exc:
            raise ValueError(f"{member}: line {reader.line_num}: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise ValueError(f"{member}: after line {reader.line_num}: not UTF-8: {exc}") from exc


def iter_table(zip_path: Path, member: str) -> Iterator[list[str]]:
    """The records of one zip member as lists of fields, the header row first. The member
    streams; a quoted ``|`` or line break stays in its field."""
    for _line, row in _iter_rows(zip_path, member):
        yield row


def _check_name(name: str, what: str) -> None:
    if not _NAME_RE.match(name) or len(name.encode("utf-8")) > _MAX_IDENT:
        raise ValueError(f"{what} {name!r} is not a plain lower-case name of at most 63 bytes")


@dataclass
class TablePlan:
    """The columns of one release table: the header's text columns, then the typed copies."""

    table: str
    columns: list[str]
    # (index of the text column, name of its typed date copy)
    typed: list[tuple[int, str]]
    report: dict[str, list[str]]

    @property
    def all_columns(self) -> list[str]:
        return [*self.columns, *(name for _, name in self.typed)]


def plan_table(table: str, header: Sequence[str], dictionary: dict[str, list[dict]]) -> TablePlan:
    """The plan of ``table`` from its header row, checked against ``dictionary``. A bad,
    empty or repeated column name raises ``ValueError``; differences from the dictionary are
    reported in ``report`` (``only_in_file``, ``only_in_dictionary``,
    ``deprecated_in_file``)."""
    _check_name(table, "table name")
    columns = [str(c) for c in header]
    if not columns:
        raise ValueError(f"{table}: the header row is empty")
    seen: set[str] = set()
    for c in columns:
        _check_name(c, f"{table}: column")
        if c in seen:
            raise ValueError(f"{table}: column {c!r} is repeated in the header")
        seen.add(c)
    typed: list[tuple[int, str]] = []
    if table in TYPED_TABLES:
        clash = [c for c in columns if c.endswith(TYPED_DATE_SUFFIX)]
        if clash:
            raise ValueError(f"{table}: {clash} end in the reserved suffix {TYPED_DATE_SUFFIX}")
        dates = set(dct.date_columns(dictionary, table))
        for i, c in enumerate(columns):
            if c in dates:
                name = c + TYPED_DATE_SUFFIX
                _check_name(name, f"{table}: typed column")
                typed.append((i, name))
    known = dct.columns(dictionary, table)
    deprecated = set(dct.deprecated_columns(dictionary, table))
    report = {
        "only_in_file": [c for c in columns if c not in set(known)],
        "only_in_dictionary": [c for c in known if c not in seen],
        "deprecated_in_file": [c for c in columns if c in deprecated],
    }
    return TablePlan(table=table, columns=columns, typed=typed, report=report)


def _parse_date(value: str) -> date | None:
    v = value.strip()
    if not _ISO_DATE_RE.match(v):
        return None
    try:
        return date.fromisoformat(v)
    except ValueError:
        return None


class RowShaper:
    """Turns the fields of one record into a COPY row of a ``TablePlan``, and counts."""

    def __init__(self, plan: TablePlan, member: str) -> None:
        self.plan = plan
        self.member = member
        self.width = len(plan.columns)
        self.stats: dict[str, Any] = {
            "rows": 0,
            "nul_fields": 0,
            "blank_lines": 0,
            "bad_dates": {},
            "empty_dates": {},
        }

    def shape(self, row: Sequence[str], *, line: int) -> tuple:
        """The COPY row; ``ValueError`` (with the member and the line) for a record with
        another width than the header."""
        if len(row) != self.width:
            raise ValueError(
                f"{self.member}: line {line}: {len(row)} fields, the header has {self.width}"
            )
        out: list[Any] = []
        for v in row:
            if "\x00" in v:
                v = v.replace("\x00", "")
                self.stats["nul_fields"] += 1
            out.append(v if v != "" else None)
        for i, _name in self.plan.typed:
            col, v = self.plan.columns[i], out[i]
            if v is None:
                bucket = self.stats["empty_dates"]
                bucket[col] = bucket.get(col, 0) + 1
                out.append(None)
                continue
            d = _parse_date(v)
            if d is None:
                bucket = self.stats["bad_dates"]
                bucket[col] = bucket.get(col, 0) + 1
            out.append(d)
        self.stats["rows"] += 1
        return tuple(out)


def row_size(row: Sequence[str]) -> int:
    """Estimated memory of one buffered row, in bytes: its text, plus ``_FIELD_OVERHEAD``
    per field (the str object) and ``_ROW_OVERHEAD`` per row (the tuple)."""
    return _ROW_OVERHEAD + sum(len(v) + _FIELD_OVERHEAD for v in row)


def compare_tables(tables: Sequence[str], dictionary: dict[str, list[dict]]) -> dict:
    """Tables of the dictionary missing from the zip, and tables of the zip that the
    dictionary does not know. Both are reported; ``unexpected_missing`` decides which
    missing table fails the load."""
    have, known = set(tables), set(dictionary)
    return {
        "missing_from_zip": sorted(known - have),
        "not_in_dictionary": sorted(have - known),
    }


def unexpected_missing(missing_from_zip: Sequence[str]) -> list[str]:
    """The tables of ``missing_from_zip`` other than ``KNOWN_ABSENT_TABLES``: each one fails
    the load."""
    return sorted(set(missing_from_zip) - set(KNOWN_ABSENT_TABLES))


def orphan_breaches(counts: dict[str, tuple[int, int]], limit: float) -> dict[str, float]:
    """``{table: share}`` of the tables whose orphan share (``orphans / rows``) is above
    ``limit``. ``counts`` is ``{table: (rows, orphans)}``; a table with no rows is left out."""
    out = {}
    for table, (rows, orphans) in sorted(counts.items()):
        if rows and orphans / rows > limit:
            out[table] = orphans / rows
    return out


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
@dataclass
class _Ctx:
    zip_path: Path
    raw_dir: Path
    key: str
    fp: str
    release_id: str
    schema: str
    database: str
    processed_root: Path
    run_id: str
    dictionary: dict[str, list[dict]]
    batch_bytes: int


def _t(schema: str, table: str) -> sql.Identifier:
    return sql.Identifier(schema, table)


def _state(ctx: _Ctx) -> sql.Identifier:
    return _t(ctx.schema, STATE_TABLE)


def _schema_exists(cur: psycopg.Cursor, schema: str) -> bool:
    cur.execute("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s)", (schema,))
    return bool(cur.fetchone()[0])


def _finished_fp(cur: psycopg.Cursor, schema: str) -> str | None:
    """The fingerprint of the ``_release`` state row of ``schema`` (a finished release),
    or ``None``."""
    cur.execute("SELECT to_regclass(%s)", (f"{schema}.{STATE_TABLE}",))
    if cur.fetchone()[0] is None:
        return None
    cur.execute(
        sql.SQL("SELECT fingerprint FROM {} WHERE table_name = %s").format(_t(schema, STATE_TABLE)),
        (RELEASE_ROW,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _set_lock_timeout(cur: psycopg.Cursor) -> int:
    seconds = get_settings().reg_lock_timeout_s
    cur.execute("SELECT set_config('lock_timeout', %s, true)", (f"{seconds}s",))
    return seconds


def _prepare_schema(
    conn: psycopg.Connection, ctx: _Ctx, *, force: bool, reason: str | None
) -> dict:
    """Decide what to do with the release schema. Returns ``{"action": "skip" | "refuse" |
    "load", ...}``; a forced reload renames a finished schema first. Commits."""
    with conn.cursor() as cur:
        exists = _schema_exists(cur, ctx.schema)
        finished = _finished_fp(cur, ctx.schema) if exists else None
    latest = latest_release(conn, DATASET)
    conn.rollback()
    out: dict[str, Any] = {"action": "load"}
    if not force:
        if finished is not None and finished != ctx.fp:
            out = {
                "action": "refuse",
                "reason": (
                    f"release {ctx.release_id} is already loaded from other input (another "
                    "size or modification time); to replace it, run the load again with "
                    '--force --reason "<why>"'
                ),
            }
        elif finished == ctx.fp and release_loaded(conn, DATASET, ctx.release_id):
            out = {"action": "skip"}
        elif latest is not None and ctx.release_id < latest:
            out = {
                "action": "refuse",
                "reason": (
                    f"release {ctx.release_id} is older than the latest release {latest}; it "
                    "would switch the aact views back. To load it anyway, run the load again "
                    'with --force --reason "<why>"'
                ),
            }
        return out
    if not reason:
        raise ValueError("force requires a reason (audited)")
    try:
        with audit_trail.deferred_mirror(conn):
            audit_trail.record(
                "force_override",
                conn=conn,
                object=f"{DATASET} {ctx.release_id}",
                reason=reason,
                new_value={"latest_release": latest, "finished": finished is not None},
                run_id=ctx.run_id,
            )
            with conn.cursor() as cur:
                seconds = _set_lock_timeout(cur)
                try:
                    if finished is not None:
                        prev = f"{ctx.schema}_prev_{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
                        cur.execute(
                            sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(
                                sql.Identifier(ctx.schema), sql.Identifier(prev)
                            )
                        )
                        out["previous_schema"] = prev
                    # An unfinished schema stays: its tables with this input's fingerprint
                    # are reused, the others reload (a forced sample run can still resume).
                except psycopg.errors.LockNotAvailable as exc:
                    raise _Invalid(
                        f"could not lock schema {ctx.schema} within {seconds} s "
                        "(EPISTEME_REG_LOCK_TIMEOUT_S)"
                    ) from exc
            audit_trail.commit(conn)
    except BaseException:
        audit_trail.rollback(conn)
        raise
    return out


def _create_schema(conn: psycopg.Connection, ctx: _Ctx) -> None:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(ctx.schema)))
        cur.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {} (table_name text PRIMARY KEY, "
                "fingerprint text NOT NULL, source_file text NOT NULL, rows bigint NOT NULL, "
                "stats jsonb NOT NULL, done_at timestamptz NOT NULL DEFAULT now())"
            ).format(_state(ctx))
        )
    conn.commit()


def _read_state(conn: psycopg.Connection, ctx: _Ctx) -> dict[str, dict]:
    """``{table: {"rows", "stats"}}`` of the tables loaded from this input."""
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT table_name, rows, stats FROM {} WHERE fingerprint = %s AND table_name <> %s"
            ).format(_state(ctx)),
            (ctx.fp, RELEASE_ROW),
        )
        rows = cur.fetchall()
    conn.rollback()
    return {name: {"rows": n, "stats": stats} for name, n, stats in rows}


def _copy(cur: psycopg.Cursor, stmt: sql.Composed, batch: list[tuple]) -> None:
    if not batch:
        return
    with cur.copy(stmt) as cp:
        for row in batch:
            cp.write_row(row)


def _load_table(conn: psycopg.Connection, ctx: _Ctx, member: str) -> dict:
    """Load one member into its table in one transaction (rows, state row, audit row).
    Returns the table's stats."""
    table = PurePosixPath(member).stem
    rows = _iter_rows(ctx.zip_path, member)
    first = next(rows, None)
    if first is None:
        raise ValueError(f"{member}: no header row")
    plan = plan_table(table, first[1], ctx.dictionary)
    shaper = RowShaper(plan, member)
    target = _t(ctx.schema, table)
    defs = [sql.SQL("{} text").format(sql.Identifier(c)) for c in plan.columns]
    defs += [sql.SQL("{} date").format(sql.Identifier(n)) for _, n in plan.typed]
    stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
        target, sql.SQL(", ").join(sql.Identifier(c) for c in plan.all_columns)
    )
    try:
        with audit_trail.deferred_mirror(conn):
            with conn.cursor() as cur:
                cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(target))
                cur.execute(
                    sql.SQL("CREATE TABLE {} ({})").format(target, sql.SQL(", ").join(defs))
                )
                batch: list[tuple] = []
                size = 0
                for line, row in rows:
                    if not row:
                        shaper.stats["blank_lines"] += 1
                        continue
                    batch.append(shaper.shape(row, line=line))
                    size += row_size(row)
                    if size >= ctx.batch_bytes:
                        _copy(cur, stmt, batch)
                        batch, size = [], 0
                _copy(cur, stmt, batch)
                stats = {**shaper.stats, "columns": plan.report, "member": member}
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {} (table_name, fingerprint, source_file, rows, stats) "
                        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (table_name) DO UPDATE SET "
                        "fingerprint = EXCLUDED.fingerprint, source_file = EXCLUDED.source_file, "
                        "rows = EXCLUDED.rows, stats = EXCLUDED.stats, done_at = now()"
                    ).format(_state(ctx)),
                    (table, ctx.fp, ctx.key, shaper.stats["rows"], Jsonb(stats)),
                )
            audit_trail.record(
                "load_commit",
                conn=conn,
                object=f"{DATASET} {ctx.release_id} {table}",
                rows_affected=shaper.stats["rows"],
                run_id=ctx.run_id,
            )
            audit_trail.commit(conn)
    except BaseException:
        audit_trail.rollback(conn)
        raise
    return {"rows": shaper.stats["rows"], "stats": stats}


def _write_marker(ctx: _Ctx, table: str, state: dict) -> None:
    """The marker of one loaded table; a current one stays as it is."""
    marker = checkpoint_markers.load_success_marker_path(
        ctx.processed_root, SOURCE, marker_name(ctx.database, ctx.zip_path, ctx.raw_dir, table)
    )
    try:
        stored = json.loads(marker.read_text(encoding="utf-8")).get("stats", {})
        if stored.get("fingerprint") == ctx.fp:
            return
    except (OSError, ValueError, AttributeError):
        pass
    checkpoint_markers.write_marker(
        marker,
        {
            "source": SOURCE,
            "source_file": ctx.key,
            "database": ctx.database,
            "release_id": ctx.release_id,
            "table": table,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "stats": {"rows": state["rows"], "fingerprint": ctx.fp},
        },
    )


def _drop_stale_tables(conn: psycopg.Connection, ctx: _Ctx, tables: Sequence[str]) -> list[str]:
    """Drop the tables of the release schema that this input does not hold (a partial load
    of other content). Never runs on a finished schema."""
    keep = {*tables, STATE_TABLE, INTERVENTION_SUBSTANCE}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind = 'r'",
            (ctx.schema,),
        )
        stale = sorted(r[0] for r in cur.fetchall() if r[0] not in keep)
        for name in stale:
            cur.execute(sql.SQL("DROP TABLE {}").format(_t(ctx.schema, name)))
        cur.execute(
            sql.SQL(
                "DELETE FROM {} WHERE table_name <> %s "
                "AND (fingerprint <> %s OR table_name <> ALL(%s))"
            ).format(_state(ctx)),
            (RELEASE_ROW, ctx.fp, list(tables)),
        )
    conn.commit()
    return stale


def _columns(conn: psycopg.Connection, schema: str) -> dict[str, list[str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = %s ORDER BY table_name, ordinal_position",
            (schema,),
        )
        out: dict[str, list[str]] = {}
        for table, column in cur.fetchall():
            out.setdefault(table, []).append(column)
    conn.rollback()
    return out


def index_plan(table: str, columns: Sequence[str]) -> list[tuple[str, int | None]]:
    """The indexes of one release table: ``(column, prefix)``, where ``prefix`` is the
    ``left(col, prefix)`` length of a free-text column and ``None`` for a column indexed
    whole (``nct_id``, ``substance_key``). Only columns the table has."""
    have = set(columns)
    wanted = (["nct_id"] if "nct_id" in have else []) + [
        c for c in EXTRA_INDEXES.get(table, ()) if c in have
    ]
    return [(c, None if c in _WHOLE_INDEX_COLUMNS else INDEX_PREFIX_CHARS) for c in wanted]


def index_statement(schema: str, table: str, column: str, prefix: int | None) -> sql.Composed:
    """``CREATE INDEX IF NOT EXISTS`` of one ``index_plan`` entry."""
    if prefix is None:
        key: sql.Composable = sql.Identifier(column)
    else:
        key = sql.SQL("left({c}, {n})").format(c=sql.Identifier(column), n=sql.Literal(prefix))
    return sql.SQL("CREATE INDEX IF NOT EXISTS {i} ON {t} ({k})").format(
        i=sql.Identifier(index_name(table, column)), t=_t(schema, table), k=key
    )


def _build_indexes(conn: psycopg.Connection, schema: str, tables: Sequence[str]) -> list[str]:
    """Indexes (``index_plan``) and ``ANALYZE``, a transaction per table. Returns the
    indexes as ``table.column`` or ``table.left(column, n)``."""
    cols = _columns(conn, schema)
    built: list[str] = []
    for table in tables:
        with conn.cursor() as cur:
            for column, prefix in index_plan(table, cols.get(table, [])):
                cur.execute(index_statement(schema, table, column, prefix))
                built.append(
                    f"{table}.{column}" if prefix is None else f"{table}.left({column}, {prefix})"
                )
            cur.execute(sql.SQL("ANALYZE {}").format(_t(schema, table)))
        conn.commit()
    return built


def member_has_data_rows(zip_path: Path, member: str) -> bool:
    """Whether ``member`` has at least one record after the header line."""
    rows = _iter_rows(zip_path, member)
    next(rows, None)
    return any(row for _line, row in rows)


def upstream_empty_tables(zip_path: Path, state: dict[str, dict]) -> list[str]:
    """The tables with zero loaded rows whose zip member has no data row (header only):
    empty upstream. A state without a member name is never counted as upstream-empty."""
    out: list[str] = []
    for table, s in state.items():
        member = (s.get("stats") or {}).get("member")
        if not s["rows"] and member and not member_has_data_rows(zip_path, member):
            out.append(table)
    return sorted(out)


def empty_tables(state: dict[str, dict], upstream_empty: Sequence[str] = ()) -> list[str]:
    """The tables with zero rows that the file ships with data rows. A table in
    ``upstream_empty`` (header-only member) is not a failure."""
    return sorted(t for t, s in state.items() if not s["rows"] and t not in upstream_empty)


def check_empty(
    state: dict[str, dict], upstream_empty: Sequence[str], allowed: Sequence[str]
) -> None:
    """``_Invalid`` when ``studies`` has no rows (an empty release is never loaded, whatever
    the zip holds), or when a table has no loaded row and is not an allowed upstream-empty
    table: only a header-only member of a table in ``allowed``
    (``Settings.aact_may_be_empty_tables``) may load with no row."""
    if STUDIES in state and not state[STUDIES]["rows"]:
        raise _Invalid("studies has no rows: the release is refused")
    tolerated = [t for t in upstream_empty if t in allowed]
    empty = empty_tables(state, tolerated)
    if empty:
        raise _Invalid(f"tables with zero rows: {', '.join(empty)}")


def _check_rows(conn: psycopg.Connection, ctx: _Ctx, state: dict[str, dict]) -> dict:
    """The row checks of spec 4.4; ``_Invalid`` on a breach. Returns ``{"studies": n,
    "orphans": {table: n}, "orphan_shares": {table: share}, "missing_nct_id": [tables]}``;
    ``missing_nct_id`` lists the tables with no ``nct_id`` column (no orphan count)."""
    if STUDIES not in state:
        raise _Invalid("the zip has no studies table")
    upstream_empty = upstream_empty_tables(ctx.zip_path, state)
    check_empty(state, upstream_empty, get_settings().aact_may_be_empty_tables)
    cols = _columns(conn, ctx.schema)
    studies = _t(ctx.schema, STUDIES)
    counts: dict[str, tuple[int, int]] = {}
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT count(*), count(DISTINCT nct_id) FROM {}").format(studies))
        rows, distinct = cur.fetchone()
        for table in sorted(state):
            if table == STUDIES or "nct_id" not in cols.get(table, []):
                continue
            cur.execute(
                sql.SQL(
                    "SELECT count(*), count(*) FILTER (WHERE t.nct_id IS NULL OR NOT EXISTS "
                    "(SELECT 1 FROM {s} s WHERE s.nct_id = t.nct_id)) FROM {t} t"
                ).format(s=studies, t=_t(ctx.schema, table))
            )
            n, orphans = cur.fetchone()
            counts[table] = (n, orphans)
    conn.rollback()
    if rows != distinct:
        raise _Invalid(f"studies has {rows} rows but {distinct} distinct nct_id")
    limit = get_settings().aact_max_orphan_share
    breaches = orphan_breaches(counts, limit)
    if breaches:
        listed = ", ".join(f"{t} {s:.4f}" for t, s in breaches.items())
        raise _Invalid(
            f"orphan nct_id share above {limit} (EPISTEME_AACT_MAX_ORPHAN_SHARE): {listed}"
        )
    return {
        "studies": rows,
        "upstream_empty_tables": upstream_empty,
        "orphans": {t: o for t, (_n, o) in counts.items()},
        "orphan_shares": {t: (o / n if n else 0.0) for t, (n, o) in counts.items()},
        "missing_nct_id": sorted(
            t for t in state if t != STUDIES and "nct_id" not in cols.get(t, [])
        ),
    }


_NAME_SOURCES = (
    # (table, id column, method)
    ("interventions", "id", "normalised_name"),
    ("intervention_other_names", "intervention_id", "normalised_other_name"),
)


def _resolve_interventions(conn: psycopg.Connection, ctx: _Ctx) -> dict:
    """``resolve_names`` over the distinct intervention names, then the release table
    ``intervention_substance``, in one transaction. Returns the name and coverage counts."""
    cols = _columns(conn, ctx.schema)
    sources = [
        (table, id_col, method)
        for table, id_col, method in _NAME_SOURCES
        if {"nct_id", "name", id_col} <= set(cols.get(table, []))
    ]
    target = _t(ctx.schema, INTERVENTION_SUBSTANCE)
    try:
        with audit_trail.deferred_mirror(conn):
            with conn.cursor() as cur:
                names: set[str] = set()
                for table, _id, _m in sources:
                    cur.execute(
                        sql.SQL("SELECT DISTINCT name FROM {} WHERE name IS NOT NULL").format(
                            _t(ctx.schema, table)
                        )
                    )
                    names.update(r[0] for r in cur.fetchall())
            counts = resolve_names(conn, sorted(names), SOURCE)
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS pg_temp._aact_name_map")
                cur.execute("CREATE TEMP TABLE _aact_name_map (raw text, norm text) ON COMMIT DROP")
                with cur.copy("COPY _aact_name_map (raw, norm) FROM STDIN") as cp:
                    for raw in names:
                        n = resolve_key(raw)
                        if n:
                            cp.write_row((raw, n))
                cur.execute("ANALYZE _aact_name_map")
                del names
                cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(target))
                cur.execute(
                    sql.SQL(
                        "CREATE TABLE {} (nct_id text, intervention_id text, "
                        "substance_key bigint, moiety_key bigint, method text)"
                    ).format(target)
                )
                for table, id_col, method in sources:
                    # moiety_key: the active moiety of the matched substance, or the
                    # substance itself when it has none (as the FAERS signal tables key on).
                    cur.execute(
                        sql.SQL(
                            "INSERT INTO {target} "
                            "(nct_id, intervention_id, substance_key, moiety_key, method) "
                            "SELECT DISTINCT i.nct_id, i.{id}, x.substance_key, "
                            "COALESCE(s.moiety_key, x.substance_key), %s::text "
                            "FROM {src} i JOIN _aact_name_map m ON m.raw = i.name "
                            "JOIN {xref} x ON x.id_type = %s AND x.source = %s "
                            "AND x.id_value = m.norm "
                            "LEFT JOIN {subst} s ON s.substance_key = x.substance_key"
                        ).format(
                            target=target,
                            id=sql.Identifier(id_col),
                            src=_t(ctx.schema, table),
                            xref=_t(REG_SCHEMA, "substance_xref"),
                            subst=_t(REG_SCHEMA, "substance"),
                        ),
                        (method, f"{SOURCE}_name", SOURCE),
                    )
                # A table made a moment ago has no statistics and no index. Without them the
                # coverage query below was planned as a nested loop over every intervention
                # (it ran 6.5 hours). Index first, then ANALYZE: the planner then sees the
                # row count and the lookup key.
                cur.execute(
                    sql.SQL("CREATE INDEX {} ON {} (nct_id, intervention_id)").format(
                        sql.Identifier(f"{INTERVENTION_SUBSTANCE}_nct_idx"), target
                    )
                )
                cur.execute(sql.SQL("ANALYZE {}").format(target))
                coverage = {"interventions": 0, "interventions_matched": 0}
                if sources and sources[0][0] == "interventions":
                    cur.execute(
                        sql.SQL(
                            "SELECT count(*), count(*) FILTER (WHERE EXISTS (SELECT 1 FROM "
                            "{target} s WHERE s.intervention_id = i.id AND s.nct_id = i.nct_id)) "
                            "FROM {src} i"
                        ).format(target=target, src=_t(ctx.schema, "interventions"))
                    )
                    total, matched = cur.fetchone()
                    coverage = {"interventions": total, "interventions_matched": matched}
                cur.execute(sql.SQL("SELECT count(*) FROM {}").format(target))
                rows = cur.fetchone()[0]
            result = {**counts, **coverage, "rows": rows}
            audit_trail.record(
                "load_commit",
                conn=conn,
                object=f"{DATASET} {ctx.release_id} {INTERVENTION_SUBSTANCE}",
                rows_affected=rows,
                new_value=result,
                run_id=ctx.run_id,
            )
            audit_trail.commit(conn)
    except BaseException:
        audit_trail.rollback(conn)
        raise
    return result


def _switch(conn: psycopg.Connection, ctx: _Ctx, tables: Sequence[str], summary: dict) -> None:
    """Views and the ``reg_release`` row, in one transaction under ``lock_timeout``."""
    view_schema = sql.Identifier(VIEW_SCHEMA)
    try:
        with audit_trail.deferred_mirror(conn):
            with conn.cursor() as cur:
                seconds = _set_lock_timeout(cur)
                try:
                    cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(view_schema))
                    cur.execute("SELECT to_regrole(%s) IS NOT NULL", (APP_ROLE,))
                    has_app = bool(cur.fetchone()[0])
                    if has_app:
                        cur.execute(
                            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                                view_schema, sql.Identifier(APP_ROLE)
                            )
                        )
                    cur.execute(
                        "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
                        "ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind = 'v' "
                        "ORDER BY 1",
                        (VIEW_SCHEMA,),
                    )
                    for (view,) in cur.fetchall():
                        cur.execute(sql.SQL("DROP VIEW {}").format(_t(VIEW_SCHEMA, view)))
                    for table in sorted(tables):
                        cur.execute(
                            sql.SQL("CREATE VIEW {v} AS SELECT * FROM {t}").format(
                                v=_t(VIEW_SCHEMA, table), t=_t(ctx.schema, table)
                            )
                        )
                        if has_app:
                            cur.execute(
                                sql.SQL("GRANT SELECT ON {} TO {}").format(
                                    _t(VIEW_SCHEMA, table), sql.Identifier(APP_ROLE)
                                )
                            )
                    cur.execute(
                        f"UPDATE {REG_SCHEMA}.reg_release SET is_latest = false "
                        "WHERE dataset = %s AND is_latest AND release_id <> %s",
                        (DATASET, ctx.release_id),
                    )
                    cur.execute(
                        f"INSERT INTO {REG_SCHEMA}.reg_release "
                        "(dataset, release_id, rows, status, is_latest) "
                        "VALUES (%s, %s, %s, 'loaded', true) "
                        "ON CONFLICT (dataset, release_id) DO UPDATE SET loaded_at = now(), "
                        "rows = EXCLUDED.rows, status = 'loaded', is_latest = true",
                        (DATASET, ctx.release_id, summary["rows"]),
                    )
                    cur.execute(
                        sql.SQL(
                            "INSERT INTO {} (table_name, fingerprint, source_file, rows, stats) "
                            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (table_name) DO UPDATE SET "
                            "fingerprint = EXCLUDED.fingerprint, rows = EXCLUDED.rows, "
                            "stats = EXCLUDED.stats, done_at = now()"
                        ).format(_state(ctx)),
                        (
                            RELEASE_ROW,
                            ctx.fp,
                            ctx.key,
                            summary["rows"],
                            Jsonb({"dictionary_date": dct.DICTIONARY_DATE}),
                        ),
                    )
                except psycopg.errors.LockNotAvailable as exc:
                    raise _Invalid(
                        f"could not lock the aact views or reg_release within {seconds} s "
                        "(EPISTEME_REG_LOCK_TIMEOUT_S); a transaction that reads them is "
                        "still open"
                    ) from exc
            audit_trail.record(
                "load_commit",
                conn=conn,
                object=f"{DATASET} {ctx.release_id}",
                rows_affected=summary["rows"],
                old_value={"latest_release": summary.get("previous_release")},
                new_value=summary,
                run_id=ctx.run_id,
            )
            audit_trail.commit(conn)
    except BaseException:
        audit_trail.rollback(conn)
        raise


def _record_failed(conn: psycopg.Connection, release_id: str) -> None:
    """A ``failed`` registry row for a first attempt; an existing row is never changed."""
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {REG_SCHEMA}.reg_release (dataset, release_id, rows, status) "
            "VALUES (%s, %s, 0, 'failed') ON CONFLICT (dataset, release_id) DO NOTHING",
            (DATASET, release_id),
        )
    conn.commit()


def _drop_replaced(conn: psycopg.Connection, ctx: _Ctx) -> list[str]:
    """After a good switch: drop the ``aact_r<id>_prev_<time>`` schemas of this release id
    (copies that a forced reload replaced; no view reads them any more). The schemas of other
    release ids are never dropped. One transaction under ``lock_timeout``; returns the
    names dropped."""
    pattern = rf"^{re.escape(ctx.schema)}_prev_[0-9]{{14}}$"
    try:
        with audit_trail.deferred_mirror(conn):
            with conn.cursor() as cur:
                _set_lock_timeout(cur)
                cur.execute(
                    "SELECT nspname FROM pg_namespace WHERE nspname ~ %s ORDER BY 1", (pattern,)
                )
                names = [r[0] for r in cur.fetchall()]
                for name in names:
                    cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
            if names:
                audit_trail.record(
                    "load_replace",
                    conn=conn,
                    object=f"{DATASET} {ctx.release_id}",
                    old_value={"dropped_schemas": names},
                    reason="replaced copy of the same release, after a forced reload",
                    run_id=ctx.run_id,
                )
            audit_trail.commit(conn)
    except BaseException:
        audit_trail.rollback(conn)
        raise
    for name in names:
        _LOG.info("dropped the replaced schema %s", name)
    return names


def load_release(
    conn: psycopg.Connection,
    zip_path: Path,
    *,
    raw_dir: Path,
    run_id: str,
    force: bool = False,
    reason: str | None = None,
) -> dict:
    """Load one AACT zip as its release and switch the ``aact`` views to it; see the module
    docstring. Owns the transactions of ``conn``, which must be idle.

    Returns ``status`` (``loaded``, ``skipped``, ``refused`` or ``failed``, with ``reason``
    for the last two), ``release_id``, ``schema``, ``source_file``, ``dictionary_date`` and,
    after a load: ``rows`` (studies), ``tables`` (``{table: {"rows", "stats"}}``),
    ``tables_loaded``, ``tables_skipped``, ``dictionary`` (``missing_from_zip``,
    ``not_in_dictionary``), ``orphans``, ``orphan_shares``, ``indexes``, ``names``
    (``resolve_names`` counts and intervention coverage), ``previous_release`` and, after a
    forced reload of a finished release, ``previous_schema``, and ``dropped_schemas`` (the
    replaced ``_prev_`` copies of this release dropped after the switch; ``drop_error`` when
    that step failed, which does not fail the load). A read or parse error raises; the
    previous release keeps serving and a re-run resumes. A database error after the tables
    first records the ``failed`` registry row, then raises.
    """
    if force and not reason:
        raise ValueError("force requires a reason (audited)")
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("load_release owns the transactions: the connection is not idle")
    zip_path = Path(zip_path)
    release_id = release_id_from_zip(zip_path)
    key = input_key(zip_path, raw_dir)
    settings = get_settings()
    ctx = _Ctx(
        zip_path=zip_path,
        raw_dir=Path(raw_dir),
        key=key,
        fp=fingerprint(zip_path, key),
        release_id=release_id,
        schema=schema_name(release_id),
        database=database_name(conn),
        processed_root=settings.processed_root,
        run_id=run_id,
        dictionary=dct.read_dictionary(),
        batch_bytes=settings.load_batch_bytes,
    )
    result: dict[str, Any] = {
        "status": "failed",
        "release_id": release_id,
        "schema": ctx.schema,
        "source_file": key,
        "dictionary_date": dct.DICTIONARY_DATE,
    }
    members = table_members(zip_path)
    order = member_order(list(members))
    # Fail fast: before a table loads, and before a forced run renames a finished schema.
    absent = unexpected_missing(compare_tables(order, ctx.dictionary)["missing_from_zip"])
    if absent:
        return {
            **result,
            "reason": (
                f"tables of the dictionary missing from the zip: {', '.join(absent)} (only "
                f"{', '.join(KNOWN_ABSENT_TABLES)} may be absent)"
            ),
        }
    try:
        decision = _prepare_schema(conn, ctx, force=force, reason=reason)
    except _Invalid as exc:
        conn.rollback()
        return {**result, "reason": str(exc)}
    if decision["action"] == "skip":
        return {**result, "status": "skipped"}
    if decision["action"] == "refuse":
        return {**result, "status": "refused", "reason": decision["reason"]}
    if "previous_schema" in decision:
        result["previous_schema"] = decision["previous_schema"]
    result["previous_release"] = latest_release(conn, DATASET)
    conn.rollback()

    _create_schema(conn, ctx)
    _drop_stale_tables(conn, ctx, order)
    state = _read_state(conn, ctx)
    loaded = skipped = 0
    for table in order:
        if table in state:
            skipped += 1
        else:
            state[table] = _load_table(conn, ctx, members[table])
            loaded += 1
        _write_marker(ctx, table, state[table])
    result.update(
        tables=state,
        tables_loaded=loaded,
        tables_skipped=skipped,
        dictionary={
            **compare_tables(order, ctx.dictionary),
            "columns": {
                t: s["stats"].get("columns") for t, s in state.items() if s["stats"].get("columns")
            },
        },
    )
    try:
        result["indexes"] = _build_indexes(conn, ctx.schema, order)
        result.update(_check_rows(conn, ctx, state))
        names = _resolve_interventions(conn, ctx)
        result["names"] = names
        _build_indexes(conn, ctx.schema, [INTERVENTION_SUBSTANCE])
        summary = {
            "rows": result["studies"],
            "previous_release": result.get("previous_release"),
            "source_file": key,
            "dictionary_date": dct.DICTIONARY_DATE,
            "dictionary": {k: v for k, v in result["dictionary"].items() if k != "columns"},
            "tables": {t: s["rows"] for t, s in state.items()},
            "orphans": result["orphans"],
            # The tables whose zip member is header-only: empty upstream, not a failed load.
            "upstream_empty_tables": result["upstream_empty_tables"],
            # The tables with no nct_id column (search_terms): no orphan count, and the
            # gate counts them as checked.
            "missing_nct_id": result["missing_nct_id"],
            "indexes": result["indexes"],
            "names": names,
        }
        _switch(conn, ctx, [*order, INTERVENTION_SUBSTANCE], summary)
    except _Invalid as exc:
        _record_failed(conn, release_id)
        result.update(status="failed", reason=str(exc))
        return result
    except (psycopg.Error, OSError, ValueError):
        try:
            _record_failed(conn, release_id)
        except psycopg.Error:
            _LOG.exception("could not record the failed release %s", release_id)
        raise
    result.update(status="loaded", rows=result["studies"], dropped_schemas=[])
    try:
        result["dropped_schemas"] = _drop_replaced(conn, ctx)
    except psycopg.Error as exc:
        conn.rollback()
        result["drop_error"] = repr(exc)
        _LOG.warning("release %s loaded, but its replaced schemas stay: %r", release_id, exc)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    from episteme.data.db import connection as db_connection
    from episteme.data.db.guard import pool_kwargs
    from episteme.ops.status import current
    from episteme.ops.stop import stage_guard

    parser = argparse.ArgumentParser(
        prog="python -m episteme.data.aact.load_aact",
        description="Load an AACT snapshot into its release schema and switch the aact views.",
    )
    parser.add_argument("--raw-dir", type=Path, default=None, help="default: <raw root>/aact")
    parser.add_argument("--zip", type=Path, default=None, help="default: the newest release zip")
    parser.add_argument("--force", action="store_true", help="replace a loaded release")
    parser.add_argument("--reason", default=None, help="required with --force (audited)")
    args = parser.parse_args(argv)
    if args.force and not args.reason:
        print("error: --force requires --reason", file=sys.stderr)
        return 2

    settings = get_settings()
    raw_dir = args.raw_dir or (settings.raw_root / SOURCE)
    external_run_id = settings.run_id
    run_id = external_run_id or f"{SOURCE}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    own_bracket = external_run_id is None
    dsn = db_connection.admin_dsn_from_settings()
    holder: list = [None]

    with stage_guard(lambda: holder[0], lock_stage="load", db_lock=True, argv=argv, dsn=dsn) as g:
        current().update(source=SOURCE)
        failure: str | None = None
        with psycopg.connect(dsn, autocommit=False, **pool_kwargs(settings)) as conn:
            holder[0] = conn
            try:
                if own_bracket:
                    audit_trail.record("run_start", conn=conn, object=SOURCE, run_id=run_id)
                    conn.commit()
                try:
                    zip_path = args.zip or find_release_zip(raw_dir, release_zips(raw_dir))
                    result = load_release(
                        conn,
                        zip_path,
                        raw_dir=raw_dir,
                        run_id=run_id,
                        force=args.force,
                        reason=args.reason,
                    )
                    status = result["status"]
                    if status in ("refused", "failed"):
                        failure = f"{result['release_id']}: {result.get('reason')}"
                    elif status == "skipped":
                        print(f"skip {SOURCE} {result['release_id']}: already loaded")
                    else:
                        names = result.get("names") or {}
                        print(
                            f"ok {SOURCE} release={result['release_id']} "
                            f"studies={result['rows']} tables={len(result['tables'])} "
                            f"tables_loaded={result['tables_loaded']} "
                            f"tables_skipped={result['tables_skipped']} "
                            f"missing_from_zip={result['dictionary']['missing_from_zip']} "
                            f"indexes={len(result['indexes'])} "
                            f"names_matched={names.get('matched')} "
                            f"names_unmatched={names.get('unmatched')} "
                            f"interventions_matched={names.get('interventions_matched')}/"
                            f"{names.get('interventions')}"
                            + (
                                f" previous_schema={result['previous_schema']}"
                                if result.get("previous_schema")
                                else ""
                            )
                        )
                        for name in result.get("dropped_schemas") or []:
                            print(
                                f"dropped {name} (replaced copy of release {result['release_id']})"
                            )
                        if result.get("drop_error"):
                            print(
                                f"WARN {SOURCE}: the replaced schemas of release "
                                f"{result['release_id']} stay: {result['drop_error']}"
                            )
                except Exception as exc:  # noqa: BLE001 - reported, and the exit code carries it
                    conn.rollback()
                    failure = repr(exc)
                if failure:
                    print(f"FAIL {SOURCE}: {failure}")
                    try:
                        record_failure(SOURCE, "load", "release", failure, run_id=run_id)
                    except Exception:  # noqa: BLE001 - the exit code still carries it
                        _LOG.exception("could not record the failure of %s", SOURCE)
                if own_bracket:
                    audit_trail.record(
                        "run_end",
                        conn=conn,
                        object=SOURCE,
                        run_id=run_id,
                        reason=f"failed: {failure}"[:500] if failure else None,
                    )
                    conn.commit()
            finally:
                holder[0] = None
        return g.exit_code(1 if failure else 0)


if __name__ == "__main__":
    raise SystemExit(main())
