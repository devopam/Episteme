"""Load FAERS by generation: readers never see a half-replaced state.

    python -m episteme.data.regulatory.faers.load [--raw-dir DIR] [--force --reason "..."]

FAERS is a full reissue, so a refresh does not edit the live tables. ``load_generation``
builds new tables and swaps them in:

1. **Tables.** ``faers_report_<gen>``, ``faers_drug_<gen>`` and ``faers_reaction_<gen>``,
   built ``LIKE`` the live parents, with the same partitions (read from the catalog) and
   no index. ``<gen>`` is ``g`` plus 10 hex digits, new for each generation. A small
   table, ``faers_gen_<gen>``, holds the progress of the generation.
2. **COPY.** One transaction per zip: the rows, the new dictionary values, the zip's
   progress row and its audit row commit together. A zip is read as a stream. Rows wait
   in a buffer of at most ``load_batch_bytes``.
3. **De-duplication,** with set-based SQL, one transaction per year partition. Among the
   copies of one id, the highest version wins, then the latest ``receiptdate``, then the
   latest file (the largest ``source_file``); a missing version or date ranks lowest. The
   other copies go by ``(safetyreportid, source_file)`` from all three tables, and are
   counted. Only duplicated ids reach the sort, so the sort's temp space follows the
   duplicates, and the ``GROUP BY`` that finds them follows one partition. A last pass does
   the same across partitions, for copies that disagree on ``receivedate``
   (``duplicates_cross_year``). That pass is not bounded by a partition: its ``GROUP BY``
   reads all ids of the generation. No index exists yet, so any pass that finds losers
   reads the whole of its child tables to delete them.
4. **Indexes** per partition, one transaction per partition, then ``ANALYZE`` of each
   parent (which also collects the parent-level statistics that autovacuum never does).
5. **``post_load(conn)``,** when given, then the **swap**: one transaction under
   ``lock_timeout`` (``EPISTEME_FAERS_LOCK_TIMEOUT_S``) renames the live parents and their
   partitions to ``<parent>_old_<gen>`` and ``<parent>_old_<gen>_y2024``, renames the new
   ones to the plain names of migration 0010 (``faers_drug``, ``faers_drug_y2024``), and
   drops the progress table. A later transaction drops the old generation. If a reader
   holds a lock, the swap fails with ``GenerationSwapError``, the old generation keeps
   serving, and the built generation stays for the next run. The index names keep their
   ``<gen>`` token after the swap (``faers_drug_<gen>_y2024_sid``); the token is new for
   each generation, so index names never collide.

**Resume: how a run decides** (``decide_generation``). The fingerprint is a SHA-256 of
``input_key``, size and modification time of every input. It is stored as the table
comment of the generation's ``faers_report`` parent, so it stays with the table through
the swap.

* An unfinished generation with this fingerprint: resume it. Zips with a progress row are
  skipped, and finished steps are not repeated.
* Otherwise, the live ``faers_report`` has this fingerprint: nothing to do (``current``).
* Otherwise: start a fresh generation.
* Every other unfinished generation is dropped. ``force`` starts fresh in every case.

Each zip also has a marker, keyed on ``input_key``, which records the generation. The
progress row, not the marker, decides the skip: it commits with the rows, so a crash
between the commit and the marker write cannot load a zip twice (a re-run writes the
missing marker). That also keeps a zip's ``(safetyreportid, source_file)`` unique, on which
the de-duplication depends: an id repeated inside one zip keeps its first copy, and the
others are counted (``duplicates_in_zip``).

**Dictionaries** ``faers_drug_entry`` and ``faers_term`` live across generations and are
never swapped. They only grow and their ids stay stable, so ``faers_entry_substance`` stays
valid. Their in-memory caches last for one zip.

**For ``post_load``:** it runs after the indexes and before the swap. A success is recorded,
so a re-run does not repeat it; a failure is run again by the next run, so it must clean
up after itself. The plain names still hold the old generation at that step, so it finds
the new tables with ``unfinished_generations(conn)`` (one entry at that step) and
``generation_table(parent, generation)``.

``load_generation`` owns the transactions of its connection, so the connection must be
idle when it is called. The loader creates and renames tables, so the CLI connects as the
table owner (``episteme_sys_admin``); the schema's default privileges then cover the new
tables. The swap replaces the parents, so a new parent must have the owner and privileges
of the live one: ``_check_access`` refuses a difference before the COPY and again before
the swap. A view or foreign key on a live parent would follow the rename to the old
generation, so none may exist. Config comes from ``episteme.config``; this module never
reads ``os.environ``.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import re
import sys
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

import ijson
import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from episteme import audit_trail
from episteme.config import get_settings
from episteme.data import checkpoint_markers
from episteme.data.regulatory.faers.parse import count_report, iter_reports, report_rows
from episteme.data.regulatory.load_reg import input_keys
from episteme.data.regulatory.load_substance import MAX_NAME_BYTES
from episteme.ops.failure import record_failure

_LOG = logging.getLogger(__name__)

SCHEMA = "episteme"
SOURCE = "openfda_faers"
PARENTS = ("faers_report", "faers_drug", "faers_reaction")
GENERATION_RE = r"g[0-9a-f]{10}"
_PARENT_RE = "(?:" + "|".join(PARENTS) + ")"
_OLD_RE = f"^{_PARENT_RE}_old_{GENERATION_RE}$"
_RANGE_BOUND = re.compile(r"FOR VALUES FROM \((-?\d+)\) TO \((-?\d+)\)")

# Per-partition indexes of spec 5.4, and the partial batch_norm index of its lot section.
INDEXES: dict[str, tuple[tuple[str, str], ...]] = {
    "faers_report": (("sid", "(safetyreportid)"),),
    "faers_drug": (
        ("entry", "(drug_entry_id)"),
        ("sid", "(safetyreportid)"),
        ("batched", "(drug_entry_id) WHERE batch_number IS NOT NULL"),
        ("lot", "(batch_norm) WHERE batch_norm IS NOT NULL"),
    ),
    "faers_reaction": (("term", "(term_id)"), ("sid", "(safetyreportid)")),
}

_REPORT_COLUMNS = (
    "safetyreportid",
    "receive_year",
    "receive_date",
    "receipt_date",
    "version",
    "serious",
    "serious_death",
    "serious_hospitalization",
    "serious_life_threatening",
    "serious_disabling",
    "serious_congenital",
    "serious_other",
    "country",
    "sex",
    "age_years",
    "weight_kg",
)
_DRUG_COLUMNS = (
    "safetyreportid",
    "seq",
    "receive_year",
    "role",
    "batch_number",
    "batch_norm",
    "route",
    "indication",
)
_REACTION_COLUMNS = ("safetyreportid", "seq", "receive_year", "outcome")
# Estimated bytes of one row beyond its text.
_ROW_OVERHEAD = 64


class GenerationError(Exception):
    """The generation cannot go live."""


class GenerationSwapError(GenerationError):
    """The swap could not take its lock. The old generation still serves."""


# ---------------------------------------------------------------------------
# Names and the resume decision (pure)
# ---------------------------------------------------------------------------
def new_generation_id() -> str:
    return "g" + uuid.uuid4().hex[:10]


def generation_table(parent: str, generation: str) -> str:
    return f"{parent}_{generation}"


def leaf_table(parent: str, generation: str, suffix: str) -> str:
    return f"{parent}_{generation}{suffix}"


def old_table(parent: str, generation: str) -> str:
    return f"{parent}_old_{generation}"


def old_leaf_table(parent: str, generation: str, suffix: str) -> str:
    return f"{parent}_old_{generation}{suffix}"


def state_table(generation: str) -> str:
    return f"faers_gen_{generation}"


def _ident(name: str) -> sql.Identifier:
    return sql.Identifier(SCHEMA, name)


def parse_bound(bound: str) -> tuple[int, int] | None:
    """``(from, to)`` of a range partition's bound text; ``None`` for the default."""
    if bound == "DEFAULT":
        return None
    match = _RANGE_BOUND.fullmatch(bound)
    if not match:
        raise ValueError(f"unexpected partition bound: {bound!r}")
    return int(match.group(1)), int(match.group(2))


def fingerprint(files: Iterable[tuple[Path, str]]) -> str:
    """SHA-256 over ``(input_key, size, mtime_ns)`` of the inputs, in key order."""
    rows = []
    for path, key in files:
        st = Path(path).stat()
        rows.append([key, st.st_size, st.st_mtime_ns])
    return hashlib.sha256(json.dumps(sorted(rows)).encode("utf-8")).hexdigest()


class Decision(NamedTuple):
    action: str  # "fresh", "resume" or "current"
    generation: str | None  # the generation to resume
    drop: list[str]  # the unfinished generations to drop


def decide_generation(
    inputs_fp: str, *, live: str | None, unfinished: Mapping[str, str | None], force: bool
) -> Decision:
    """What a run does; see the module docstring."""
    if force:
        return Decision("fresh", None, sorted(unfinished))
    match = next((g for g, fp in sorted(unfinished.items()) if fp == inputs_fp), None)
    drop = sorted(g for g in unfinished if g != match)
    if match is not None:
        return Decision("resume", match, drop)
    return Decision("current" if live == inputs_fp else "fresh", None, drop)


class Batch:
    """The rows of one COPY round, up to about ``cap_bytes`` of row data."""

    def __init__(self, cap_bytes: int) -> None:
        self.cap_bytes = cap_bytes
        self.clear()

    def clear(self) -> None:
        self.reports: list[dict] = []
        self.drugs: list[dict] = []
        self.reactions: list[dict] = []
        self.bytes = 0

    @staticmethod
    def _size(row: dict) -> int:
        return _ROW_OVERHEAD + sum(len(v) for v in row.values() if isinstance(v, str))

    def add(self, report: dict, drugs: list[dict], reactions: list[dict]) -> None:
        self.reports.append(report)
        self.drugs.extend(drugs)
        self.reactions.extend(reactions)
        self.bytes += self._size(report)
        self.bytes += sum(self._size(r) for r in drugs)
        self.bytes += sum(self._size(r) for r in reactions)

    @property
    def full(self) -> bool:
        return self.bytes >= self.cap_bytes


def faers_zips(raw_dir: Path) -> list[Path]:
    """The zips under ``<raw>/drug/event/``, in path order. Nothing else is read."""
    base = Path(raw_dir) / "drug" / "event"
    return sorted(p for p in base.rglob("*.zip") if p.is_file()) if base.is_dir() else []


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
def _relations(cur: psycopg.Cursor, pattern: str) -> list[str]:
    cur.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind = 'p' AND c.relname ~ %s ORDER BY 1",
        (SCHEMA, pattern),
    )
    return [r[0] for r in cur.fetchall()]


def _comment(cur: psycopg.Cursor, table: str) -> str | None:
    cur.execute(
        "SELECT obj_description(to_regclass(%s::text), 'pg_class')",
        (f"{SCHEMA}.{table}",),
    )
    return cur.fetchone()[0]


def unfinished_generations(conn: psycopg.Connection) -> dict[str, str | None]:
    """generation -> fingerprint of the generations that were not swapped in."""
    out: dict[str, str | None] = {}
    with conn.cursor() as cur:
        for name in _relations(cur, f"^faers_report_{GENERATION_RE}$"):
            out[name.removeprefix("faers_report_")] = _comment(cur, name)
    return out


def _children(cur: psycopg.Cursor, parent: str) -> list[tuple[str, str]]:
    """``(suffix, bound)`` of the partitions of ``parent``, for example ``_y2004``."""
    cur.execute(
        "SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) "
        "FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        "WHERE i.inhparent = to_regclass(%s::text) ORDER BY 1",
        (f"{SCHEMA}.{parent}",),
    )
    out = []
    for name, bound in cur.fetchall():
        if not name.startswith(f"{parent}_"):
            raise GenerationError(f"partition {name} does not start with {parent}_")
        out.append((name[len(parent) :], bound))
    return out


# ---------------------------------------------------------------------------
# Generation tables and progress
# ---------------------------------------------------------------------------
def _create_generation(conn: psycopg.Connection, generation: str, inputs_fp: str) -> None:
    with conn.cursor() as cur:
        for parent in PARENTS:
            new = generation_table(parent, generation)
            cur.execute(
                sql.SQL(
                    "CREATE TABLE {new} (LIKE {parent} INCLUDING ALL EXCLUDING INDEXES) "
                    "PARTITION BY RANGE (receive_year)"
                ).format(new=_ident(new), parent=_ident(parent))
            )
            for suffix, bound in _children(cur, parent):
                values = parse_bound(bound)
                spec = (
                    sql.SQL("DEFAULT")
                    if values is None
                    else sql.SQL("FOR VALUES FROM ({}) TO ({})").format(
                        sql.Literal(values[0]), sql.Literal(values[1])
                    )
                )
                cur.execute(
                    sql.SQL("CREATE TABLE {leaf} PARTITION OF {new} {spec}").format(
                        leaf=_ident(leaf_table(parent, generation, suffix)),
                        new=_ident(new),
                        spec=spec,
                    )
                )
        cur.execute(
            sql.SQL("COMMENT ON TABLE {t} IS {fp}").format(
                t=_ident(generation_table("faers_report", generation)), fp=sql.Literal(inputs_fp)
            )
        )
        cur.execute(
            sql.SQL(
                "CREATE TABLE {t} (kind text NOT NULL, key text NOT NULL, stats jsonb NOT NULL, "
                "PRIMARY KEY (kind, key))"
            ).format(t=_ident(state_table(generation)))
        )
    conn.commit()


def _read_state(conn: psycopg.Connection, generation: str) -> dict[tuple[str, str], dict]:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT kind, key, stats FROM {t}").format(t=_ident(state_table(generation)))
        )
        out = {(kind, key): stats for kind, key, stats in cur.fetchall()}
    conn.commit()
    return out


def _put_state(cur: psycopg.Cursor, generation: str, kind: str, key: str, stats: dict) -> None:
    cur.execute(
        sql.SQL("INSERT INTO {t} (kind, key, stats) VALUES (%s, %s, %s)").format(
            t=_ident(state_table(generation))
        ),
        (kind, key, Jsonb(stats)),
    )


def _sum_stats(state: dict[tuple[str, str], dict], kind: str) -> Counter:
    total: Counter = Counter()
    for (k, _), stats in state.items():
        if k == kind:
            total.update(stats)
    return total


def _drop_tables(conn: psycopg.Connection, names: Sequence[str]) -> bool:
    """Drop ``names`` under the lock timeout. ``False``, with a warning, when that fails."""
    if not names:
        return True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('lock_timeout', %s, true)",
                (f"{get_settings().faers_lock_timeout_s}s",),
            )
            cur.execute(
                sql.SQL("DROP TABLE IF EXISTS {}").format(
                    sql.SQL(", ").join(_ident(n) for n in names)
                )
            )
        conn.commit()
    except psycopg.Error as exc:
        with contextlib.suppress(psycopg.Error):
            conn.rollback()
        _LOG.warning("could not drop %s (the next run tries again): %r", ", ".join(names), exc)
        return False
    return True


def _drop_generation(conn: psycopg.Connection, generation: str) -> None:
    names = [generation_table(p, generation) for p in PARENTS] + [state_table(generation)]
    if not _drop_tables(conn, names):
        raise GenerationError(f"could not drop the unfinished generation {generation}")


def _drop_old_leftovers(conn: psycopg.Connection) -> bool:
    with conn.cursor() as cur:
        names = _relations(cur, _OLD_RE)
    conn.commit()
    return _drop_tables(conn, names)


# ---------------------------------------------------------------------------
# COPY of one zip
# ---------------------------------------------------------------------------
def _entry_md5(alias: str = "") -> str:
    """The key expression of ``faers_drug_entry_uq`` (migration 0010), exactly."""
    a = f"{alias}." if alias else ""
    return (
        f"md5(coalesce({a}product_name, '') || chr(31) || coalesce({a}active_substance, '') "
        f"|| chr(31) || coalesce({a}annotation, ''))"
    )


def _entry_key(drug: dict) -> tuple[str | None, str | None, str | None]:
    return (drug["product_name"], drug["active_substance"], drug["annotation"])


def _create_stage(cur: psycopg.Cursor) -> None:
    cur.execute(
        "CREATE TEMP TABLE _faers_new_entry (k integer, product_name text, "
        "active_substance text, annotation text) ON COMMIT DROP"
    )
    cur.execute("CREATE TEMP TABLE _faers_new_term (term text) ON COMMIT DROP")


def _resolve_entries(cur: psycopg.Cursor, drugs: list[dict], cache: dict) -> None:
    """Put the new combinations of ``drugs`` into the dictionary, and their ids in ``cache``."""
    new = list(dict.fromkeys(k for k in map(_entry_key, drugs) if any(k) and k not in cache))
    if not new:
        return
    cur.execute("TRUNCATE _faers_new_entry")
    with cur.copy(
        "COPY _faers_new_entry (k, product_name, active_substance, annotation) FROM STDIN"
    ) as cp:
        for i, key in enumerate(new):
            cp.write_row((i, *key))
    cur.execute("ANALYZE _faers_new_entry")
    cur.execute(
        f"INSERT INTO {SCHEMA}.faers_drug_entry (product_name, active_substance, annotation) "
        "SELECT product_name, active_substance, annotation FROM _faers_new_entry "
        f"ON CONFLICT (({_entry_md5()})) DO NOTHING"
    )
    cur.execute(
        f"SELECT t.k, e.drug_entry_id FROM _faers_new_entry t "
        f"JOIN {SCHEMA}.faers_drug_entry e ON {_entry_md5('e')} = {_entry_md5('t')}"
    )
    found = cur.fetchall()
    if len(found) != len(new):
        raise GenerationError(f"{len(new)} dictionary entries written, {len(found)} found")
    for k, entry_id in found:
        cache[new[k]] = entry_id


def _usable_term(term: str | None) -> bool:
    return bool(term) and len(term.encode("utf-8")) <= MAX_NAME_BYTES


def _resolve_terms(cur: psycopg.Cursor, reactions: list[dict], cache: dict) -> None:
    new = list(
        dict.fromkeys(
            r["term"] for r in reactions if _usable_term(r["term"]) and r["term"] not in cache
        )
    )
    if not new:
        return
    cur.execute("TRUNCATE _faers_new_term")
    with cur.copy("COPY _faers_new_term (term) FROM STDIN") as cp:
        for term in new:
            cp.write_row((term,))
    cur.execute("ANALYZE _faers_new_term")
    cur.execute(
        f"INSERT INTO {SCHEMA}.faers_term (term) SELECT term FROM _faers_new_term "
        "ON CONFLICT (term) DO NOTHING"
    )
    cur.execute(
        f"SELECT t.term, e.term_id FROM _faers_new_term t "
        f"JOIN {SCHEMA}.faers_term e ON e.term = t.term"
    )
    cache.update(cur.fetchall())


def _copy_stmt(table: str, columns: Sequence[str]) -> sql.Composed:
    return sql.SQL("COPY {t} ({cols}) FROM STDIN").format(
        t=_ident(table), cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    )


def _flush(
    cur: psycopg.Cursor,
    generation: str,
    source_file: str,
    batch: Batch,
    entries: dict,
    terms: dict,
) -> None:
    """One COPY round. The dictionaries first: the rows carry their ids."""
    _resolve_entries(cur, batch.drugs, entries)
    _resolve_terms(cur, batch.reactions, terms)
    if batch.reports:
        stmt = _copy_stmt(
            generation_table("faers_report", generation), (*_REPORT_COLUMNS, "source_file")
        )
        with cur.copy(stmt) as cp:
            for r in batch.reports:
                cp.write_row((*(r[c] for c in _REPORT_COLUMNS), source_file))
    if batch.drugs:
        stmt = _copy_stmt(
            generation_table("faers_drug", generation),
            (*_DRUG_COLUMNS, "drug_entry_id", "source_file"),
        )
        with cur.copy(stmt) as cp:
            for r in batch.drugs:
                entry_id = entries.get(_entry_key(r))
                cp.write_row((*(r[c] for c in _DRUG_COLUMNS), entry_id, source_file))
    if batch.reactions:
        stmt = _copy_stmt(
            generation_table("faers_reaction", generation),
            (*_REACTION_COLUMNS, "term_id", "source_file"),
        )
        with cur.copy(stmt) as cp:
            for r in batch.reactions:
                term_id = terms.get(r["term"])
                cp.write_row((*(r[c] for c in _REACTION_COLUMNS), term_id, source_file))
    batch.clear()


def _load_zip(
    conn: psycopg.Connection, generation: str, path: Path, key: str, run_id: str
) -> dict[str, int]:
    """Load one zip in one transaction. The caches and the id set last for this zip only."""
    counts: Counter = Counter(
        reports=0, drug_rows=0, reaction_rows=0, no_date=0, no_id=0, duplicates_in_zip=0
    )
    counts["long_terms"] = 0
    entries: dict = {}
    terms: dict = {}
    seen: set[str] = set()
    batch = Batch(get_settings().load_batch_bytes)
    try:
        with audit_trail.deferred_mirror(conn):
            with conn.cursor() as cur:
                _create_stage(cur)
                for report in iter_reports(path):
                    row, drugs, reactions = report_rows(report)
                    report_id = row["safetyreportid"]
                    if report_id is None:
                        counts["no_id"] += 1
                        continue
                    if report_id in seen:
                        counts["duplicates_in_zip"] += 1
                        continue
                    seen.add(report_id)
                    count_report(counts, row)
                    counts["reports"] += 1
                    counts["drug_rows"] += len(drugs)
                    counts["reaction_rows"] += len(reactions)
                    counts["long_terms"] += sum(
                        1 for r in reactions if r["term"] and not _usable_term(r["term"])
                    )
                    batch.add(row, drugs, reactions)
                    if batch.full:
                        _flush(cur, generation, key, batch, entries, terms)
                _flush(cur, generation, key, batch, entries, terms)
                _put_state(cur, generation, "zip", key, dict(counts))
            audit_trail.record(
                "load_commit",
                conn=conn,
                object=key,
                rows_affected=counts["reports"],
                run_id=run_id,
            )
            audit_trail.commit(conn)
    except BaseException:
        audit_trail.rollback(conn)
        raise
    return dict(counts)


def _write_zip_marker(
    processed_dir: Path, path: Path, key: str, generation: str, stats: dict
) -> None:
    """The marker of a zip this generation has loaded; a current one stays as it is."""
    marker = checkpoint_markers.load_success_marker_path(processed_dir, SOURCE, key)
    with contextlib.suppress(OSError, ValueError, AttributeError):
        stored = json.loads(marker.read_text(encoding="utf-8")).get("stats", {})
        if stored.get("generation") == generation:
            return
    st = path.stat()
    checkpoint_markers.write_marker(
        marker,
        {
            "source": SOURCE,
            "source_file": key,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "stats": {**stats, "generation": generation, "file": [st.st_size, st.st_mtime_ns]},
        },
    )


# ---------------------------------------------------------------------------
# De-duplication
# ---------------------------------------------------------------------------
def _remove_losers(
    cur: psycopg.Cursor, scan: sql.Composable, targets: Sequence[sql.Composable]
) -> dict[str, int]:
    """Keep the winner of each id in ``scan``; delete the other copies from ``targets``
    (report, drug, reaction). Returns the rows removed per table."""
    out = {"reports": 0, "drug_rows": 0, "reaction_rows": 0}
    cur.execute("DROP TABLE IF EXISTS pg_temp._faers_dups, pg_temp._faers_losers")
    cur.execute(
        sql.SQL(
            "CREATE TEMP TABLE _faers_dups ON COMMIT DROP AS "
            "SELECT safetyreportid FROM {scan} GROUP BY safetyreportid HAVING count(*) > 1"
        ).format(scan=scan)
    )
    cur.execute("ANALYZE _faers_dups")
    cur.execute(
        sql.SQL(
            "CREATE TEMP TABLE _faers_losers ON COMMIT DROP AS "
            "SELECT safetyreportid, source_file FROM ("
            "  SELECT r.safetyreportid, r.source_file, row_number() OVER ("
            "    PARTITION BY r.safetyreportid "
            "    ORDER BY r.version DESC NULLS LAST, r.receipt_date DESC NULLS LAST, "
            "             r.source_file DESC) AS rn "
            "  FROM {scan} r JOIN _faers_dups d USING (safetyreportid)"
            ") ranked WHERE rn > 1"
        ).format(scan=scan)
    )
    cur.execute("SELECT count(*) FROM _faers_losers")
    if cur.fetchone()[0] == 0:
        return out
    cur.execute("ANALYZE _faers_losers")
    for name, target in zip(out, targets, strict=True):
        cur.execute(
            sql.SQL(
                "DELETE FROM {t} x USING _faers_losers l "
                "WHERE x.safetyreportid = l.safetyreportid AND x.source_file = l.source_file"
            ).format(t=target)
        )
        out[name] = cur.rowcount
    return out


def _dedupe(conn: psycopg.Connection, generation: str) -> None:
    """Per year partition, then across partitions. One transaction each; a finished
    partition has a progress row, so a re-run does not repeat it and keeps its counts."""
    state = _read_state(conn, generation)
    with conn.cursor() as cur:
        suffixes = [s for s, _ in _children(cur, generation_table("faers_report", generation))]
    conn.commit()
    for suffix in suffixes:
        if ("dedup", suffix) in state:
            continue
        with conn.cursor() as cur:
            removed = _remove_losers(
                cur,
                _ident(leaf_table("faers_report", generation, suffix)),
                [_ident(leaf_table(p, generation, suffix)) for p in PARENTS],
            )
            _put_state(cur, generation, "dedup", suffix, removed)
        conn.commit()
    if ("dedup_cross_year", "all") not in state:
        with conn.cursor() as cur:
            removed = _remove_losers(
                cur,
                _ident(generation_table("faers_report", generation)),
                [_ident(generation_table(p, generation)) for p in PARENTS],
            )
            _put_state(cur, generation, "dedup_cross_year", "all", removed)
        conn.commit()


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------
def _build_indexes(conn: psycopg.Connection, generation: str) -> None:
    """The indexes per partition, one transaction per partition, then ``ANALYZE``."""
    for parent in PARENTS:
        with conn.cursor() as cur:
            suffixes = [s for s, _ in _children(cur, generation_table(parent, generation))]
        conn.commit()
        for leaf_suffix in suffixes:
            leaf = leaf_table(parent, generation, leaf_suffix)
            with conn.cursor() as cur:
                for suffix, spec in INDEXES[parent]:
                    cur.execute(
                        sql.SQL("CREATE INDEX IF NOT EXISTS {idx} ON {t} {spec}").format(
                            idx=sql.Identifier(f"{leaf}_{suffix}"),
                            t=_ident(leaf),
                            spec=sql.SQL(spec),
                        )
                    )
            conn.commit()
    with conn.cursor() as cur:
        for parent in PARENTS:
            cur.execute(
                sql.SQL("ANALYZE {t}").format(t=_ident(generation_table(parent, generation)))
            )
        _put_state(cur, generation, "phase", "indexed", {})
    conn.commit()


# ---------------------------------------------------------------------------
# Checks and the swap
# ---------------------------------------------------------------------------
def _check_rows(conn: psycopg.Connection, generation: str, expected: int) -> dict[str, int]:
    """Stop before the swap when the new generation is empty or short."""
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT count(*) FROM {t}").format(
                t=_ident(generation_table("faers_report", generation))
            )
        )
        reports = cur.fetchone()[0]
        cur.execute(
            sql.SQL(
                "SELECT count(*), count(*) FILTER (WHERE receive_year IS NULL) FROM {t}"
            ).format(t=_ident(leaf_table("faers_report", generation, "_default")))
        )
        in_default, no_date = cur.fetchone()
    conn.commit()
    if reports == 0:
        raise GenerationError("the new generation has no reports; it is not swapped in")
    if reports != expected:
        raise GenerationError(f"{reports} reports stored, {expected} expected; no swap")
    return {"reports": reports, "reports_in_default": in_default, "reports_no_date": no_date}


def _access(cur: psycopg.Cursor, table: str) -> tuple[str, set[str]]:
    cur.execute(
        "SELECT relowner::regrole::text, relacl::text[] FROM pg_class "
        "WHERE oid = to_regclass(%s::text)",
        (f"{SCHEMA}.{table}",),
    )
    owner, acl = cur.fetchone()
    return owner, set(acl or ())


def _check_access(cur: psycopg.Cursor, generation: str) -> None:
    """Readers keep their access through a swap only if the parents match."""
    for parent in PARENTS:
        live = _access(cur, parent)
        new = _access(cur, generation_table(parent, generation))
        if live != new:
            raise GenerationError(
                f"{generation_table(parent, generation)} differs from {parent} in owner or "
                f"privileges ({new} against {live}); no swap. Connect as the table owner, or "
                "repair the grants, and run again with --force."
            )


def _rename(cur: psycopg.Cursor, name: str, new_name: str) -> None:
    cur.execute(
        sql.SQL("ALTER TABLE {t} RENAME TO {new}").format(
            t=_ident(name), new=sql.Identifier(new_name)
        )
    )


def _swap(
    conn: psycopg.Connection, generation: str, inputs_fp: str, reports: int, run_id: str
) -> None:
    """Rename the generation in, in one transaction, under the lock timeout."""
    seconds = get_settings().faers_lock_timeout_s
    try:
        with audit_trail.deferred_mirror(conn):
            with conn.cursor() as cur:
                _check_access(cur, generation)
                cur.execute("SELECT set_config('lock_timeout', %s, true)", (f"{seconds}s",))
                try:
                    cur.execute(
                        sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(
                            sql.SQL(", ").join(_ident(p) for p in PARENTS)
                        )
                    )
                except psycopg.errors.LockNotAvailable as exc:
                    raise GenerationSwapError(
                        f"could not lock the live FAERS tables within {seconds} s "
                        "(EPISTEME_FAERS_LOCK_TIMEOUT_S): a transaction that reads them is "
                        f"still open. The old generation still serves. Generation {generation} "
                        "is complete: run the load again to swap it in."
                    ) from exc
                for parent in PARENTS:
                    new = generation_table(parent, generation)
                    for suffix, _ in _children(cur, parent):
                        _rename(
                            cur, f"{parent}{suffix}", old_leaf_table(parent, generation, suffix)
                        )
                    _rename(cur, parent, old_table(parent, generation))
                    for suffix, _ in _children(cur, new):
                        _rename(cur, leaf_table(parent, generation, suffix), f"{parent}{suffix}")
                    _rename(cur, new, parent)
                cur.execute(sql.SQL("DROP TABLE {t}").format(t=_ident(state_table(generation))))
            audit_trail.record(
                "load_replace",
                conn=conn,
                object=f"{SOURCE} {generation}",
                rows_affected=reports,
                new_value={"generation": generation, "fingerprint": inputs_fp},
                run_id=run_id,
            )
            audit_trail.commit(conn)
    except BaseException:
        audit_trail.rollback(conn)
        raise
    _LOG.info(
        "swapped in %s: the parents and partitions now carry the plain names; the old "
        "ones are %s; the index names keep the token",
        generation,
        old_table("faers_report", generation),
    )


def load_generation(
    conn: psycopg.Connection,
    zips: Sequence[Path],
    *,
    raw_dir: Path,
    processed_dir: Path,
    run_id: str,
    post_load: Callable[[psycopg.Connection], None] | None = None,
    force: bool = False,
) -> dict:
    """Build a generation from ``zips`` and swap it in; see the module docstring.

    Commits on ``conn``, which must be idle. Returns ``status`` (``swapped`` or
    ``current``), ``generation``, ``zips_loaded``, ``zips_skipped``,
    ``old_generation_dropped`` and, when swapped: ``reports_read``, ``drug_rows_read``,
    ``reaction_rows_read`` (after the id checks of each zip), ``reports``, ``drug_rows``,
    ``reaction_rows`` (after de-duplication), ``no_date``, ``no_id``, ``long_terms``,
    ``duplicates_in_zip``, ``duplicates_removed`` (all rows of ``faers_report`` removed),
    ``duplicates_cross_year`` (those removed across partitions), ``reports_in_default``,
    ``reports_no_date``.

    Raises on a failure, and the old generation keeps serving: a zip that does not
    parse, a failed ``post_load``, ``GenerationError`` from a check,
    ``GenerationSwapError`` from the swap's lock.
    """
    paths = [Path(p) for p in zips]
    if not paths:
        raise ValueError("load_generation needs at least one input file")
    files = sorted(zip(input_keys(paths, raw_dir), paths, strict=True))
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise GenerationError("load_generation owns the transactions: the connection is not idle")
    inputs_fp = fingerprint((p, k) for k, p in files)
    _LOG.info("ijson backend: %s; %d zips", ijson.backend, len(files))

    leftovers_dropped = _drop_old_leftovers(conn)
    with conn.cursor() as cur:
        live = _comment(cur, "faers_report")
    decision = decide_generation(
        inputs_fp, live=live, unfinished=unfinished_generations(conn), force=force
    )
    conn.commit()
    for stale in decision.drop:
        _LOG.info("dropping the unfinished generation %s", stale)
        _drop_generation(conn, stale)
    if decision.action == "current":
        _LOG.info("the live generation already has these inputs")
        return {
            "status": "current",
            "generation": None,
            "zips_loaded": 0,
            "zips_skipped": len(files),
            "old_generation_dropped": leftovers_dropped,
        }
    generation = decision.generation or new_generation_id()
    if decision.action == "fresh":
        _create_generation(conn, generation, inputs_fp)
    try:
        with conn.cursor() as cur:
            _check_access(cur, generation)  # before hours of COPY
    finally:
        conn.rollback()
    _LOG.info("generation %s: %s", generation, decision.action)

    state = _read_state(conn, generation)
    loaded = skipped = 0
    for key, path in files:
        stats = state.get(("zip", key))
        if stats is not None:
            skipped += 1
        else:
            stats = _load_zip(conn, generation, path, key, run_id)
            state["zip", key] = stats
            loaded += 1
        _write_zip_marker(processed_dir, path, key, generation, stats)

    _dedupe(conn, generation)
    state = _read_state(conn, generation)
    read = _sum_stats(state, "zip")
    in_year = _sum_stats(state, "dedup")
    cross = _sum_stats(state, "dedup_cross_year")
    removed = in_year + cross
    checked = _check_rows(conn, generation, read["reports"] - removed["reports"])

    if ("phase", "indexed") not in state:
        _build_indexes(conn, generation)
    if post_load is not None and ("phase", "post_load") not in state:
        try:
            post_load(conn)
            with conn.cursor() as cur:
                _put_state(cur, generation, "phase", "post_load", {})
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    _swap(conn, generation, inputs_fp, checked["reports"], run_id)
    dropped = _drop_tables(conn, [old_table(p, generation) for p in PARENTS])
    return {
        "status": "swapped",
        "generation": generation,
        "zips_loaded": loaded,
        "zips_skipped": skipped,
        "old_generation_dropped": dropped,
        "reports_read": read["reports"],
        "drug_rows_read": read["drug_rows"],
        "reaction_rows_read": read["reaction_rows"],
        "drug_rows": read["drug_rows"] - removed["drug_rows"],
        "reaction_rows": read["reaction_rows"] - removed["reaction_rows"],
        "no_date": read["no_date"],
        "no_id": read["no_id"],
        "long_terms": read["long_terms"],
        "duplicates_in_zip": read["duplicates_in_zip"],
        "duplicates_removed": removed["reports"],
        "duplicates_cross_year": cross["reports"],
        **checked,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    from episteme.data.db import connection as db_connection
    from episteme.data.db.guard import pool_kwargs
    from episteme.ops.status import current
    from episteme.ops.stop import stage_guard

    parser = argparse.ArgumentParser(
        prog="python -m episteme.data.regulatory.faers.load",
        description="Build a new FAERS generation from drug/event zips, and swap it in.",
    )
    parser.add_argument("--raw-dir", type=Path, default=None, help="default: <raw root>/openfda")
    parser.add_argument("--force", action="store_true", help="build a new generation in any case")
    parser.add_argument("--reason", default=None, help="required with --force (audited)")
    args = parser.parse_args(argv)
    if args.force and not args.reason:
        print("error: --force requires --reason", file=sys.stderr)
        return 2

    settings = get_settings()
    raw_dir = args.raw_dir or (settings.raw_root / "openfda")
    zips = faers_zips(raw_dir)
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
                if args.force:
                    audit_trail.record(
                        "force_override",
                        conn=conn,
                        object=SOURCE,
                        reason=args.reason,
                        run_id=run_id,
                    )
                    conn.commit()
                try:
                    if not zips:
                        raise GenerationError(f"no input files under {raw_dir / 'drug' / 'event'}")
                    result = load_generation(
                        conn,
                        zips,
                        raw_dir=raw_dir,
                        processed_dir=settings.processed_root,
                        run_id=run_id,
                        force=args.force,
                    )
                    print("done " + " ".join(f"{k}={v}" for k, v in result.items()))
                except Exception as exc:  # noqa: BLE001 - reported, and the exit code carries it
                    conn.rollback()
                    failure = str(exc) if isinstance(exc, GenerationError) else repr(exc)
                    print(f"FAIL {SOURCE}: {failure}")
                    try:
                        record_failure(SOURCE, "load", "generation", failure, run_id=run_id)
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
