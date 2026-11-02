"""FAERS generation mechanics: names, the resume decision, the catalog, progress, drops,
de-duplication, indexes, checks and the swap.

The steps, the refusals and the resume rules are described in ``faers.load``, which runs
them. The COPY of one zip is in ``faers.zip_load``.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sys
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from episteme import audit_trail
from episteme.config import get_settings

_LOG = logging.getLogger(__name__)

SCHEMA = "episteme"
SOURCE = "openfda_faers"
PARENTS = ("faers_report", "faers_drug", "faers_reaction")
# Rebuilt per generation, and swapped with it (``signals.rebuild_signals``).
SIGNAL_TABLES = (
    "faers_pair_quarter",
    "faers_event_quarter",
    "faers_quarter_totals",
    "faers_batch_coverage",
    "faers_batch_quarter",
)
GENERATION_RE = r"g[0-9a-f]{10}"
_PARENT_RE = "(?:" + "|".join(PARENTS) + ")"
_SIGNAL_RE = "(?:" + "|".join(SIGNAL_TABLES) + ")"
_OLD_RE = f"^(?:{_PARENT_RE}|{_SIGNAL_RE})_old_{GENERATION_RE}$"
_RANGE_BOUND = re.compile(r"FOR VALUES FROM \((-?\d+)\) TO \((-?\d+)\)")
_PART_NAME = re.compile(r"drug-event-(\d+)-of-(\d+)\.json\.zip")
_QUARTER_NAME = re.compile(r"(\d{4})q([1-4])")
# Part numbers or quarters shown in one refusal.
_MAX_PARTS_SHOWN = 10

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


class GenerationError(Exception):
    """The generation cannot go live."""


class GenerationSwapError(GenerationError):
    """The swap could not take its lock. The old generation still serves."""


class InputSetError(GenerationError):
    """The input files are not a whole issue. Nothing was done."""


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


def faers_zips(raw_dir: Path) -> list[Path]:
    """The zips under ``<raw>/drug/event/``, in path order. Nothing else is read."""
    base = Path(raw_dir) / "drug" / "event"
    return sorted(p for p in base.rglob("*.zip") if p.is_file()) if base.is_dir() else []


def _shown(parts: Iterable[int]) -> str:
    listed = sorted(parts)
    more = f" and {len(listed) - _MAX_PARTS_SHOWN} more" if len(listed) > _MAX_PARTS_SHOWN else ""
    return ", ".join(map(str, listed[:_MAX_PARTS_SHOWN])) + more


def _missing_quarters(folders: Iterable[str]) -> list[str]:
    """The quarters absent between the earliest and the latest of ``folders``. A folder not
    named like ``2020q1`` (openFDA's ``all_other``) is outside this check."""
    present = set()
    for folder in folders:
        match = _QUARTER_NAME.fullmatch(folder)
        if match:
            present.add(int(match.group(1)) * 4 + int(match.group(2)) - 1)
    if not present:
        return []
    absent = set(range(min(present), max(present) + 1)) - present
    return [f"{n // 4}q{n % 4 + 1}" for n in sorted(absent)]


def check_input_set(paths: Iterable[Path]) -> list[str]:
    """The ways in which ``paths`` is not a whole issue, by name alone; empty when each
    quarter (the parent folder) holds exactly ``1..N of N``, with one ``N``, and no quarter
    is missing between the earliest and the latest present."""
    paths = [Path(p) for p in paths]
    problems: list[str] = []
    absent = _missing_quarters(p.parent.name for p in paths)
    if absent:
        more = (
            f" and {len(absent) - _MAX_PARTS_SHOWN} more" if len(absent) > _MAX_PARTS_SHOWN else ""
        )
        problems.append(f"missing quarters: {', '.join(absent[:_MAX_PARTS_SHOWN])}{more}")
    quarters: dict[str, list[tuple[int, int]]] = {}
    for path in paths:
        match = _PART_NAME.fullmatch(path.name)
        if match is None:
            problems.append(f"{path.parent.name}/{path.name}: not an openFDA part name")
            continue
        parts = quarters.setdefault(path.parent.name, [])
        parts.append((int(match.group(1)), int(match.group(2))))
    for quarter, parts in sorted(quarters.items()):
        totals = {total for _, total in parts}
        if len(totals) > 1:
            problems.append(f"{quarter}: several totals ({_shown(totals)}), so a stale issue")
            continue
        total = totals.pop()
        numbers = [part for part, _ in parts]
        missing = set(range(1, total + 1)) - set(numbers)
        extra = set(numbers) - set(range(1, total + 1))
        if missing or extra or len(numbers) != len(set(numbers)):
            what = []
            if missing:
                what.append(f"missing parts {_shown(missing)}")
            if extra:
                what.append(f"parts above the total: {_shown(extra)}")
            if len(numbers) != len(set(numbers)):
                what.append("a part twice")
            problems.append(f"{quarter}: of {total}, " + "; ".join(what))
    return problems


def below_floor(reports: int, live_reports: int, share: float) -> bool:
    """Whether a new generation is too small to replace the live one. An empty live
    generation sets no floor."""
    return live_reports > 0 and reports < live_reports * share


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
def _relations(cur: psycopg.Cursor, pattern: str) -> list[str]:
    cur.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relkind IN ('p', 'r') AND c.relname ~ %s ORDER BY 1",
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
    names = [generation_table(t, generation) for t in (*PARENTS, *SIGNAL_TABLES)]
    names.append(state_table(generation))
    if not _drop_tables(conn, names):
        raise GenerationError(f"could not drop the unfinished generation {generation}")


def _drop_old_generation(conn: psycopg.Connection, generation: str) -> bool:
    """Drop what the swap renamed. The new generation is live either way, so a failure
    does not fail the run; it warns, and says what to do."""
    names = [old_table(t, generation) for t in (*PARENTS, *SIGNAL_TABLES)]
    dropped = _drop_tables(conn, names)
    if not dropped:
        print(
            f"WARNING: generation {generation} is live, but the old tables were not dropped "
            f"and still use disk space: {', '.join(names)}. To retry, run the load again "
            "when no transaction holds them: each run first drops what a swap left behind.",
            file=sys.stderr,
        )
    return dropped


def _drop_old_leftovers(conn: psycopg.Connection) -> bool:
    """What a swap left behind, and signal tables of a live rebuild that stopped: those
    carry a token that no unfinished generation has."""
    with conn.cursor() as cur:
        names = _relations(cur, _OLD_RE)
        built = _relations(cur, f"^{_SIGNAL_RE}_{GENERATION_RE}$")
    keep = unfinished_generations(conn)
    conn.commit()
    names += [name for name in built if name.rsplit("_", 1)[1] not in keep]
    return _drop_tables(conn, names)


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


def _live_reports(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT count(*) FROM {t}").format(t=_ident("faers_report")))
        count = cur.fetchone()[0]
    conn.commit()
    return count


def _audit_override(
    conn: psycopg.Connection, what: str, reason: str, detail: dict, run_id: str
) -> None:
    audit_trail.record(
        "force_override",
        conn=conn,
        object=f"{SOURCE} {what}",
        new_value=detail,
        reason=reason,
        run_id=run_id,
    )
    conn.commit()


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
    _check_tables(cur, generation, (*PARENTS, *_built_signals(cur, generation)))


def _check_tables(cur: psycopg.Cursor, generation: str, tables: Sequence[str]) -> None:
    for parent in tables:
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


def _built_signals(cur: psycopg.Cursor, token: str) -> list[str]:
    """The signal tables built under ``token``: all of them, or none."""
    built = [t for t in SIGNAL_TABLES if _relations(cur, f"^{generation_table(t, token)}$")]
    if built and len(built) != len(SIGNAL_TABLES):
        raise GenerationError(f"only some signal tables exist for {token}: {built}; no swap")
    return built


def _swap_signals(cur: psycopg.Cursor, token: str) -> bool:
    """In the caller's transaction: rename the signal tables of ``token`` in, and the live
    ones to ``<table>_old_<token>``. With none built, empty the live ones instead, so that
    old signals never sit beside new rows. ``True`` when new tables went in."""
    live = sql.SQL(", ").join(_ident(t) for t in SIGNAL_TABLES)
    if not _built_signals(cur, token):
        cur.execute(sql.SQL("TRUNCATE {}").format(live))
        return False
    cur.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(live))
    for table in SIGNAL_TABLES:
        _rename(cur, table, old_table(table, token))
        _rename(cur, generation_table(table, token), table)
    return True


def swap_live_signals(
    conn: psycopg.Connection, token: str, counts: dict, run_id: str | None
) -> None:
    """Rename signal tables built from the live generation in, in one transaction under the
    lock timeout, then drop the old ones. Raises ``GenerationSwapError`` when a reader holds
    a lock: the old tables keep serving."""
    seconds = get_settings().faers_lock_timeout_s
    try:
        with audit_trail.deferred_mirror(conn):
            with conn.cursor() as cur:
                _check_tables(cur, token, _built_signals(cur, token))
                cur.execute("SELECT set_config('lock_timeout', %s, true)", (f"{seconds}s",))
                try:
                    _swap_signals(cur, token)
                except (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected) as exc:
                    raise GenerationSwapError(
                        f"could not lock the FAERS signal tables within {seconds} s "
                        "(EPISTEME_FAERS_LOCK_TIMEOUT_S). The old signal tables still serve."
                    ) from exc
            audit_trail.record(
                "load_replace",
                conn=conn,
                object=f"{SOURCE} signals {token}",
                new_value=counts,
                run_id=run_id,
            )
            audit_trail.commit(conn)
    except BaseException:
        audit_trail.rollback(conn)
        raise
    _drop_tables(conn, [old_table(t, token) for t in SIGNAL_TABLES])


def _swap(
    conn: psycopg.Connection,
    generation: str,
    inputs_fp: str,
    counts: dict[str, int],
    live: dict,
    run_id: str,
) -> None:
    """Rename the generation in, in one transaction, under the lock timeout. The swap
    drops the progress table, so its audit row keeps the counts (``counts``, and ``live``:
    the fingerprint and report count of the generation that it replaces)."""
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
                    _swap_signals(cur, generation)
                    for parent in PARENTS:
                        new = generation_table(parent, generation)
                        for suffix, _ in _children(cur, parent):
                            _rename(
                                cur,
                                f"{parent}{suffix}",
                                old_leaf_table(parent, generation, suffix),
                            )
                        _rename(cur, parent, old_table(parent, generation))
                        for suffix, _ in _children(cur, new):
                            _rename(
                                cur, leaf_table(parent, generation, suffix), f"{parent}{suffix}"
                            )
                        _rename(cur, new, parent)
                except (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected) as exc:
                    raise GenerationSwapError(
                        f"could not lock the live FAERS tables within {seconds} s "
                        "(EPISTEME_FAERS_LOCK_TIMEOUT_S), or a deadlock stopped the swap: a "
                        "transaction that reads them is still open. The old generation still "
                        f"serves. Generation {generation} is complete: run the load again to "
                        "swap it in."
                    ) from exc
                cur.execute(sql.SQL("DROP TABLE {t}").format(t=_ident(state_table(generation))))
            audit_trail.record(
                "load_replace",
                conn=conn,
                object=f"{SOURCE} {generation}",
                rows_affected=counts["reports"],
                old_value=live,
                new_value={"generation": generation, "fingerprint": inputs_fp, **counts},
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
