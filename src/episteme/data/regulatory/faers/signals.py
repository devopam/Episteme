"""FAERS signals: the bridge from dictionary entries to substances, and the signal tables.

``bridge_entries`` fills ``faers_entry_substance``. ``rebuild_signals`` builds the signal tables.
All of it is exact counting on stored rows; no statistic is computed here.

**Bridge.** For each entry, the first of these that finds any substance wins, by exact
match: the UNIIs of the annotation (``annotation``), the names of ``active_substance``
(``active_substance``), the name of the product (``product_name``). A combination maps to all
its ingredients. A name that several substances carry maps to all of them. An entry with no
match stays out, is counted, and is tried again on the next run, because the xref may have
grown. Names are normalised here with ``norm_name``, as the xref's are: SQL ``lower`` differs
from ``casefold``.

**Counts.** The first key column of a signal table holds a moiety key: ``moiety_key``, or the
``substance_key`` of a substance that has no moiety, so that no report is lost. Salt and base
are one moiety in one report. ``suspect`` means role 1. ``any`` means all roles, so interacting
counts in ``any`` only. The quarter comes from ``receive_date``, the first receipt. A report
with no usable date is counted (``reports_no_date``) and left out.

* ``faers_pair_quarter``: both directions. Row ``(a, b)`` has ``reports_any``, the reports that
  list both, and ``reports_suspect``, those where a is suspect and b has any role. The two
  directions differ in ``reports_suspect``. A report with more than ``pair_drug_cap`` distinct
  moieties is left out and counted in ``faers_quarter_totals.reports_over_pair_cap``.
* ``faers_event_quarter``: one row per case-folded term (``faers_term.term_norm``) under the
  lowest ``term_id`` that has it, because the case differs by era.
* ``faers_batch_coverage``: drug rows per moiety and quarter, and those with a real lot
  (``is_real_lot``). A combination entry counts once per moiety. Placeholders are counted apart.
* ``faers_batch_quarter``: reports per moiety, real lot and quarter.
* ``faers_moiety_quarter`` and ``faers_term_quarter``: the margins of a PRR or ROR, reports per
  moiety and reports per case-folded term. With ``faers_quarter_totals.reports_listed`` and
  ``reports_with_reaction``, a statistic can choose its universe.

**Bounds.** One transaction per year partition. Reports get an integer id in a temporary
table, and each report's moieties are one array, so pairs come from ``unnest`` with no join and
the temporary space follows one partition. A quarter lies in one partition, so the partitions
need no merge. The primary keys come last and sort the whole of each table once.

**Where the tables go.** They are built under a token name (``faers_pair_quarter_<token>``).
With ``generation``, the token is the generation, the rows come from its tables, and its swap
renames them. With no generation, the rows come from the live tables, and the rename happens at
once: the route after an improved bridge. A failed live build leaves its tables, and the next
build drops them first.

**Resume, on the generation route.** Each partition writes a ``signals`` progress row in its own
transaction, and a re-run skips it. A table that has its primary key keeps it. A finished build
writes ``SIGNALS_COMPLETE``, and ``_swap`` renames only such tables. Tables are dropped and
created only when no partition has finished. A change of the pair cap restarts the build.

Commits on ``conn``, which must be idle. Config comes from ``episteme.config``.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter

import psycopg
from psycopg import sql

from episteme.config import get_settings
from episteme.data.regulatory.faers.generation import (
    PARENTS,
    SCHEMA,
    SIGNAL_TABLES,
    SIGNALS_COMPLETE,
    GenerationError,
    _children,
    _drop_old_leftovers,
    _drop_tables,
    _ident,
    _put_state,
    _read_state,
    generation_table,
    new_generation_id,
    state_table,
    swap_live_signals,
)
from episteme.data.regulatory.normalize import PLACEHOLDER_LOTS, norm_name, split_names

_LOG = logging.getLogger(__name__)

# Rank -> faers_entry_substance.method. A lower rank goes first.
_METHODS = {1: "annotation", 2: "active_substance", 3: "product_name"}
# FAERS joins the ingredients of a combination with a backslash.
_INGREDIENT_SEP = re.compile(r"\\")
_PRIMARY_KEYS = {
    "faers_pair_quarter": ("substance_key_a", "quarter", "substance_key_b"),
    "faers_event_quarter": ("substance_key", "term_id", "quarter"),
    "faers_quarter_totals": ("quarter",),
    "faers_batch_coverage": ("substance_key", "quarter"),
    "faers_batch_quarter": ("substance_key", "batch_norm", "quarter"),
    "faers_moiety_quarter": ("substance_key", "quarter"),
    "faers_term_quarter": ("term_id", "quarter"),
}
_COUNT_KEYS = (
    "reports",
    "reports_no_date",
    "reports_listed",
    "reports_with_reaction",
    "reports_over_pair_cap",
    "drug_rows",
    "drug_rows_unbridged",
    "real_batches",
    "placeholder_batches",
    "pair_rows",
    "event_rows",
)


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------
def entry_candidates(
    entry_id: int, product: str | None, active: str | None, annotation: str | None
) -> list[tuple[int, int, str, str]]:
    """``(entry, rank, xref id_type, value)`` for each value that could resolve one entry."""
    out: dict[tuple[int, int, str, str], None] = {}
    if annotation:
        try:
            uniis = json.loads(annotation).get("unii") or []
        except (ValueError, AttributeError):
            uniis = []
        for unii in uniis:
            out[(entry_id, 1, "unii", str(unii))] = None
    if active:
        for part in split_names(_INGREDIENT_SEP.sub(";", active)):
            name = norm_name(part)
            if name:
                out[(entry_id, 2, "name", name)] = None
    if product:
        name = norm_name(product)
        if name:
            out[(entry_id, 3, "name", name)] = None
    return list(out)


def _bridge_batch(cur: psycopg.Cursor, rows: list[tuple], redo: bool) -> Counter:
    """Resolve one batch of entries in the caller's transaction."""
    cur.execute("DROP TABLE IF EXISTS pg_temp._bridge_cand, pg_temp._bridge_hit")
    cur.execute(
        "CREATE TEMP TABLE _bridge_cand (drug_entry_id bigint, rank int, id_type text, "
        "id_value text) ON COMMIT DROP"
    )
    with cur.copy("COPY _bridge_cand FROM STDIN") as cp:
        for row in rows:
            for candidate in entry_candidates(*row):
                cp.write_row(candidate)
    cur.execute("ANALYZE _bridge_cand")
    if redo:
        cur.execute(
            f"DELETE FROM {SCHEMA}.faers_entry_substance WHERE drug_entry_id = ANY(%s::bigint[])",
            ([row[0] for row in rows],),
        )
    cur.execute(
        "CREATE TEMP TABLE _bridge_hit ON COMMIT DROP AS "
        "SELECT c.drug_entry_id, c.rank, x.substance_key FROM _bridge_cand c "
        f"JOIN {SCHEMA}.substance_xref x ON x.id_type = c.id_type AND x.id_value = c.id_value"
    )
    cur.execute(
        f"INSERT INTO {SCHEMA}.faers_entry_substance (drug_entry_id, substance_key, method) "
        "SELECT DISTINCT h.drug_entry_id, h.substance_key, "
        "(%s::jsonb ->> h.rank::text) FROM _bridge_hit h "
        "JOIN (SELECT drug_entry_id, min(rank) AS rank FROM _bridge_hit GROUP BY 1) best "
        "USING (drug_entry_id, rank) "
        "ON CONFLICT (drug_entry_id, substance_key) DO NOTHING",
        (json.dumps(_METHODS),),
    )
    cur.execute(
        "SELECT rank, count(*) FROM "
        "(SELECT drug_entry_id, min(rank) AS rank FROM _bridge_hit GROUP BY 1) best GROUP BY 1"
    )
    return Counter({_METHODS[rank]: count for rank, count in cur.fetchall()})


def bridge_entries(conn: psycopg.Connection, *, redo: bool = False) -> dict:
    """Fill ``faers_entry_substance`` from the entries with no row there yet; with ``redo``,
    from all entries, replacing their rows. One transaction per batch
    (``EPISTEME_FAERS_BRIDGE_BATCH_ROWS``), so a stopped run goes on from its last commit.
    Returns ``entries`` (read), the three methods (entries resolved by each), ``unmatched``."""
    batch_rows = get_settings().faers_bridge_batch_rows
    filter_new = sql.SQL(
        ""
        if redo
        else f"AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.faers_entry_substance b "
        "WHERE b.drug_entry_id = e.drug_entry_id)"
    )
    query = sql.SQL(
        "SELECT e.drug_entry_id, e.product_name, e.active_substance, e.annotation "
        "FROM {t} e WHERE e.drug_entry_id > %s {filter} ORDER BY e.drug_entry_id LIMIT %s"
    ).format(t=_ident("faers_drug_entry"), filter=filter_new)
    found: Counter = Counter()
    entries = 0
    last = -1
    while True:
        with conn.cursor() as cur:
            cur.execute(query, (last, batch_rows))
            rows = cur.fetchall()
            if not rows:
                conn.rollback()
                break
            found += _bridge_batch(cur, rows, redo)
        conn.commit()
        entries += len(rows)
        last = rows[-1][0]
    out = {"entries": entries, **{m: found[m] for m in _METHODS.values()}}
    out["unmatched"] = entries - sum(found.values())
    return out


# ---------------------------------------------------------------------------
# The signal tables
# ---------------------------------------------------------------------------
def _fill_term_norms(conn: psycopg.Connection) -> int:
    """``term_norm`` of the terms that have none. The dictionary is small (MedDRA terms and
    their case variants), so this reads them into memory."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT term_id, term FROM {SCHEMA}.faers_term WHERE term_norm IS NULL")
        rows = cur.fetchall()
        if rows:
            cur.execute("CREATE TEMP TABLE _term_fill (term_id bigint, n text) ON COMMIT DROP")
            with cur.copy("COPY _term_fill FROM STDIN") as cp:
                for term_id, term in rows:
                    cp.write_row((term_id, norm_name(term)))
            cur.execute(
                f"UPDATE {SCHEMA}.faers_term t SET term_norm = f.n FROM _term_fill f "
                "WHERE t.term_id = f.term_id"
            )
    conn.commit()
    return len(rows)


def _create_tables(conn: psycopg.Connection, token: str) -> None:
    """New, empty signal tables with no index. Drops what a failed build left."""
    names = [generation_table(t, token) for t in SIGNAL_TABLES]
    if not _drop_tables(conn, names):
        raise GenerationError(f"could not drop the signal tables of {token}")
    with conn.cursor() as cur:
        for table in SIGNAL_TABLES:
            cur.execute(
                sql.SQL("CREATE TABLE {new} (LIKE {live} INCLUDING ALL EXCLUDING INDEXES)").format(
                    new=_ident(generation_table(table, token)), live=_ident(table)
                )
            )
    conn.commit()


def _create_maps(conn: psycopg.Connection) -> None:
    """Session tables that all partitions read: entry to moiety, term to its lowest id."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS pg_temp._sig_entry, pg_temp._sig_term")
        cur.execute(
            "CREATE TEMP TABLE _sig_entry AS "
            "SELECT DISTINCT b.drug_entry_id, COALESCE(s.moiety_key, s.substance_key) AS moiety "
            f"FROM {SCHEMA}.faers_entry_substance b "
            f"JOIN {SCHEMA}.substance s USING (substance_key)"
        )
        cur.execute(
            "CREATE TEMP TABLE _sig_term AS "
            "SELECT term_id, min(term_id) OVER (PARTITION BY term_norm) AS canon "
            f"FROM {SCHEMA}.faers_term"
        )
        cur.execute("ANALYZE _sig_entry")
        cur.execute("ANALYZE _sig_term")
    conn.commit()


def _drop_maps(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS pg_temp._sig_entry, pg_temp._sig_term")
    conn.commit()


def _build_partition(
    cur: psycopg.Cursor,
    leaves: dict[str, sql.Composable],
    new: dict[str, sql.Composable],
    cap: int,
) -> Counter:
    """All signal rows of one year partition. Returns its counts."""
    placeholders = sorted(PLACEHOLDER_LOTS)
    out: Counter = Counter()
    cur.execute(
        sql.SQL("SELECT count(*) FILTER (WHERE receive_date IS NULL) FROM {r}").format(
            r=leaves["faers_report"]
        )
    )
    out["reports_no_date"] = cur.fetchone()[0]
    cur.execute(
        sql.SQL(
            "SELECT count(*), "
            "count(*) FILTER (WHERE d.batch_norm = ANY(%s::text[])), "
            "count(*) FILTER (WHERE d.batch_norm <> ALL(%s::text[])), "
            "count(*) FILTER (WHERE e.drug_entry_id IS NULL) "
            "FROM {d} d LEFT JOIN (SELECT DISTINCT drug_entry_id FROM _sig_entry) e "
            "ON e.drug_entry_id = d.drug_entry_id"
        ).format(d=leaves["faers_drug"]),
        (placeholders, placeholders),
    )
    drug_rows, placeholder, real, unbridged = cur.fetchone()
    out.update(
        drug_rows=drug_rows,
        placeholder_batches=placeholder,
        real_batches=real,
        drug_rows_unbridged=unbridged,
    )
    # An integer id per report keeps the later tables narrow.
    cur.execute(
        sql.SQL(
            "CREATE TEMP TABLE _sig_report ON COMMIT DROP AS "
            "SELECT row_number() OVER () AS rid, safetyreportid, "
            "date_trunc('quarter', receive_date::timestamp)::date AS quarter "
            "FROM {r} WHERE receive_date IS NOT NULL"
        ).format(r=leaves["faers_report"])
    )
    cur.execute("ANALYZE _sig_report")
    cur.execute(
        sql.SQL(
            "CREATE TEMP TABLE _sig_drug ON COMMIT DROP AS "
            "SELECT r.rid, r.quarter, e.moiety, d.role, d.batch_norm FROM _sig_report r "
            "JOIN {d} d USING (safetyreportid) JOIN _sig_entry e USING (drug_entry_id)"
        ).format(d=leaves["faers_drug"])
    )
    cur.execute("ANALYZE _sig_drug")
    cur.execute(
        sql.SQL(
            "INSERT INTO {t} (substance_key, quarter, drug_rows, with_batch) "
            "SELECT moiety, quarter, count(*), "
            "count(*) FILTER (WHERE batch_norm <> ALL(%s::text[])) "
            "FROM _sig_drug GROUP BY 1, 2"
        ).format(t=new["faers_batch_coverage"]),
        (placeholders,),
    )
    cur.execute(
        sql.SQL(
            "INSERT INTO {t} (substance_key, batch_norm, quarter, reports) "
            "SELECT moiety, batch_norm, quarter, count(DISTINCT rid) FROM _sig_drug "
            "WHERE batch_norm <> ALL(%s::text[]) GROUP BY 1, 2, 3"
        ).format(t=new["faers_batch_quarter"]),
        (placeholders,),
    )
    # One row per report and moiety: salt and base are one.
    cur.execute(
        "CREATE TEMP TABLE _sig_moiety ON COMMIT DROP AS "
        "SELECT rid, quarter, moiety, bool_or(COALESCE(role = 'suspect', false)) AS suspect "
        "FROM _sig_drug GROUP BY 1, 2, 3"
    )
    cur.execute("ANALYZE _sig_moiety")
    cur.execute(
        "CREATE TEMP TABLE _sig_listed ON COMMIT DROP AS "
        "SELECT quarter, array_agg(moiety) AS moieties, array_agg(suspect) AS suspects "
        "FROM _sig_moiety GROUP BY rid, quarter"
    )
    cur.execute(
        sql.SQL(
            "INSERT INTO {t} (substance_key_a, substance_key_b, quarter, reports_suspect, "
            "reports_any) "
            "SELECT a.key, b.key, r.quarter, count(*) FILTER (WHERE a.suspect), count(*) "
            "FROM _sig_listed r "
            "CROSS JOIN LATERAL unnest(r.moieties, r.suspects) AS a(key, suspect) "
            "CROSS JOIN LATERAL unnest(r.moieties) AS b(key) "
            "WHERE cardinality(r.moieties) <= %s::int AND a.key <> b.key "
            "GROUP BY 1, 3, 2"
        ).format(t=new["faers_pair_quarter"]),
        (cap,),
    )
    out["pair_rows"] = cur.rowcount
    cur.execute(
        sql.SQL(
            "INSERT INTO {t} (substance_key, quarter, reports_suspect, reports_any) "
            "SELECT moiety, quarter, count(*) FILTER (WHERE suspect), count(*) "
            "FROM _sig_moiety GROUP BY 1, 2"
        ).format(t=new["faers_moiety_quarter"])
    )
    cur.execute(
        sql.SQL(
            "CREATE TEMP TABLE _sig_reaction ON COMMIT DROP AS "
            "SELECT DISTINCT r.rid, t.canon FROM _sig_report r "
            "JOIN {x} x USING (safetyreportid) JOIN _sig_term t ON t.term_id = x.term_id"
        ).format(x=leaves["faers_reaction"])
    )
    cur.execute("ANALYZE _sig_reaction")
    cur.execute(
        sql.SQL(
            "INSERT INTO {t} (term_id, quarter, reports) "
            "SELECT x.canon, r.quarter, count(*) "
            "FROM _sig_reaction x JOIN _sig_report r USING (rid) GROUP BY 1, 2"
        ).format(t=new["faers_term_quarter"])
    )
    cur.execute(
        sql.SQL(
            "INSERT INTO {t} (quarter, reports, reports_over_pair_cap, reports_listed, "
            "reports_with_reaction) "
            "SELECT r.quarter, r.reports, COALESCE(l.over_cap, 0), COALESCE(l.listed, 0), "
            "COALESCE(x.with_reaction, 0) "
            "FROM (SELECT quarter, count(*) AS reports FROM _sig_report GROUP BY 1) r "
            "LEFT JOIN (SELECT quarter, count(*) AS listed, "
            "           count(*) FILTER (WHERE cardinality(moieties) > %s::int) AS over_cap "
            "           FROM _sig_listed GROUP BY 1) l USING (quarter) "
            "LEFT JOIN (SELECT r2.quarter, count(DISTINCT x2.rid) AS with_reaction "
            "           FROM _sig_reaction x2 JOIN _sig_report r2 USING (rid) GROUP BY 1) x "
            "USING (quarter)"
        ).format(t=new["faers_quarter_totals"]),
        (cap,),
    )
    cur.execute("SELECT count(*) FROM _sig_report")
    out["reports"] = cur.fetchone()[0]
    cur.execute(
        "SELECT count(*), COALESCE(sum((cardinality(moieties) > %s::int)::int), 0) "
        "FROM _sig_listed",
        (cap,),
    )
    out["reports_listed"], out["reports_over_pair_cap"] = cur.fetchone()
    cur.execute("SELECT count(DISTINCT rid) FROM _sig_reaction")
    out["reports_with_reaction"] = cur.fetchone()[0]
    cur.execute(
        sql.SQL(
            "INSERT INTO {t} (substance_key, term_id, quarter, reports_suspect, reports_any) "
            "SELECT m.moiety, x.canon, m.quarter, count(*) FILTER (WHERE m.suspect), count(*) "
            "FROM _sig_moiety m JOIN _sig_reaction x USING (rid) GROUP BY 1, 2, 3"
        ).format(t=new["faers_event_quarter"])
    )
    out["event_rows"] = cur.rowcount
    return out


def _has_primary_key(cur: psycopg.Cursor, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM pg_index WHERE indisprimary AND indrelid = to_regclass(%s::text)",
        (f"{SCHEMA}.{table}",),
    )
    return cur.fetchone() is not None


def _add_primary_key(conn: psycopg.Connection, table: str, name: str) -> None:
    """The key of ``table`` on its token table ``name``, and its statistics, in one transaction,
    so a table with a key is finished."""
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("ALTER TABLE {t} ADD PRIMARY KEY ({cols})").format(
                t=_ident(name),
                cols=sql.SQL(", ").join(sql.Identifier(c) for c in _PRIMARY_KEYS[table]),
            )
        )
        cur.execute(sql.SQL("ANALYZE {t}").format(t=_ident(name)))
    conn.commit()


def _finish_tables(conn: psycopg.Connection, token: str) -> None:
    """The primary keys and the statistics, where absent. One transaction per table."""
    for table in SIGNAL_TABLES:
        name = generation_table(table, token)
        with conn.cursor() as cur:
            done = _has_primary_key(cur, name)
        conn.commit()
        if not done:
            _add_primary_key(conn, table, name)


def _finished_partitions(
    conn: psycopg.Connection, generation: str, cap: int
) -> tuple[dict | None, dict[str, dict]]:
    """``(the counts of a finished build, the counts per finished partition)``. Work done under
    another cap does not count: its rows are cleared, and the build starts over."""
    state = _read_state(conn, generation)
    complete = state.get(SIGNALS_COMPLETE)
    done = {key: stats for (kind, key), stats in state.items() if kind == "signals"}
    caps = {stats.get("pair_drug_cap") for stats in done.values()}
    if complete is not None:
        caps.add(complete.get("pair_drug_cap"))
    if caps <= {cap}:
        return complete, done
    _LOG.info("signal work under another pair cap (%s) is discarded: now %s", caps, cap)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("DELETE FROM {t} WHERE kind = 'signals' OR (kind = %s AND key = %s)").format(
                t=_ident(state_table(generation))
            ),
            SIGNALS_COMPLETE,
        )
    conn.commit()
    return None, {}


def rebuild_signals(
    conn: psycopg.Connection, *, pair_drug_cap: int, generation: str | None = None
) -> dict:
    """Build the signal tables; see the module docstring. Commits on ``conn``.

    Returns counts: ``reports`` (dated), ``reports_no_date``, ``reports_listed`` (with a bridged
    drug), ``reports_with_reaction``, ``reports_over_pair_cap``, ``drug_rows``,
    ``drug_rows_unbridged``, ``real_batches``, ``placeholder_batches`` (drug rows), ``pair_rows``,
    ``event_rows``, ``pair_drug_cap``.
    """
    token = generation or new_generation_id()
    done: dict[str, dict] = {}
    if generation is None:
        _drop_old_leftovers(conn)
    else:
        complete, done = _finished_partitions(conn, generation, pair_drug_cap)
        if complete is not None:
            return complete
    _fill_term_norms(conn)
    if not done:
        _create_tables(conn, token)
    sources = {p: generation_table(p, generation) if generation else p for p in PARENTS}
    new = {t: _ident(generation_table(t, token)) for t in SIGNAL_TABLES}
    with conn.cursor() as cur:
        suffixes = [s for s, _ in _children(cur, sources["faers_report"])]
    conn.commit()
    pending = [s for s in suffixes if s not in done]
    totals: Counter = Counter()
    for stats in done.values():
        totals.update({k: stats.get(k, 0) for k in _COUNT_KEYS})
    if pending:
        _create_maps(conn)
    try:
        for suffix in pending:
            leaves = {p: _ident(f"{sources[p]}{suffix}") for p in PARENTS}
            with conn.cursor() as cur:
                counts = _build_partition(cur, leaves, new, pair_drug_cap)
                if generation is not None:
                    row = {k: counts[k] for k in _COUNT_KEYS}
                    row["pair_drug_cap"] = pair_drug_cap
                    _put_state(cur, generation, "signals", suffix, row)
            conn.commit()
            totals.update(counts)
    except BaseException:
        conn.rollback()
        raise
    if pending:
        _drop_maps(conn)
    _finish_tables(conn, token)
    out = {k: totals[k] for k in _COUNT_KEYS}
    out["pair_drug_cap"] = pair_drug_cap
    if generation is None:
        swap_live_signals(conn, token, out, get_settings().run_id)
    else:
        with conn.cursor() as cur:
            _put_state(cur, generation, *SIGNALS_COMPLETE, out)
        conn.commit()
    _LOG.info("signal tables built under %s: %s", token, out)
    return out
