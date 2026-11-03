#!/usr/bin/env python3
"""Query-plan checks of the regulatory tables (spec 8). Read only.

    uv run python scripts/data/regulatory/check_plans.py [--dataset NAME] [--release-id ID]
        [--moiety-key N] [--other-moiety-key N]

Prints ``EXPLAIN`` for the six queries of spec section 8 and fails (exit 1) on a plan
that does not have the shape the spec asks for. Run it on a sample database, and again on
full data. For production use ``EPISTEME_DB_MODE=read-only``: the settings route
(``episteme.config``, the DB-mode guard) is the same as for the loaders.

It writes nothing. The connection and its transaction are read only, and ``EXPLAIN``
without ``ANALYZE`` does not run the statement. ``enable_seqscan`` is turned off inside the
transaction (``SET LOCAL``), so a small sample table does not hide an index plan: the check
asks whether the index can serve the query, not whether the planner prefers it on tiny data.

The values go into the SQL text as literals (``dataset``, ``release_id``, keys, dates), so
the planner prunes partitions at plan time. ``dataset`` and ``release_id`` are values read
first (``latest_release``), never a join to ``reg_release``: a join would make the planner
open every partition.

The parts that read a plan are pure functions (``plan_relations``, ``plan_indexes`` and the
checks), tested with canned ``EXPLAIN (FORMAT JSON)`` output.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[3] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

SCHEMA = "episteme"
RAW_FAERS_TABLES = ("faers_report", "faers_drug", "faers_reaction")
PLACEHOLDER = 0


# ---------------------------------------------------------------------------
# Reading a plan (pure)
# ---------------------------------------------------------------------------
def _nodes(plan: dict) -> list[dict]:
    out = [plan]
    for child in plan.get("Plans") or []:
        out.extend(_nodes(child))
    return out


def root_plan(explain_json: Any) -> dict:
    """The root node of ``EXPLAIN (FORMAT JSON)`` output (a list with one ``Plan``)."""
    if isinstance(explain_json, str):
        explain_json = json.loads(explain_json)
    return explain_json[0]["Plan"]


def plan_relations(explain_json: Any) -> list[str]:
    """The relations (tables, partitions) that the plan scans, in plan order, one entry per
    scan node. A partition that pruning removed does not appear."""
    return [n["Relation Name"] for n in _nodes(root_plan(explain_json)) if n.get("Relation Name")]


def plan_indexes(explain_json: Any) -> list[str]:
    return [n["Index Name"] for n in _nodes(root_plan(explain_json)) if n.get("Index Name")]


def _under(names: list[str], prefix: str) -> list[str]:
    return sorted({n for n in names if n == prefix or n.startswith(prefix + "_")})


# ---------------------------------------------------------------------------
# The six queries and their checks
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Params:
    dataset: str
    release_id: str
    moiety_key: int
    other_moiety_key: int
    names: tuple[str, ...]
    window_a: tuple[date, date]  # [start, end) of the first window
    window_b: tuple[date, date]
    years: tuple[int, int]  # the last two years, first and last


@dataclass(frozen=True)
class Check:
    name: str
    build: Callable[[Params], sql.Composable]
    verify: Callable[[Any, Params], list[str]]


def q_latest_release(p: Params) -> sql.Composable:
    return sql.SQL(
        "SELECT record_key, event_date FROM {t} WHERE dataset = {d} AND release_id = {r} "
        "AND substance_names && {n}::text[]"
    ).format(
        t=sql.Identifier(SCHEMA, "reg_record"),
        d=sql.Literal(p.dataset),
        r=sql.Literal(p.release_id),
        n=sql.Literal(list(p.names)),
    )


def v_latest_release(plan: Any, p: Params) -> list[str]:
    leaves = _under(plan_relations(plan), "reg_record")
    if len(leaves) > 1:
        return [f"reads {len(leaves)} reg_record partitions ({', '.join(leaves)}); expected 1"]
    return []


def q_pair_counts(p: Params) -> sql.Composable:
    (a0, a1), (b0, b1) = p.window_a, p.window_b
    return sql.SQL(
        "SELECT substance_key_b, "
        "sum(reports_suspect) FILTER (WHERE quarter >= {a0} AND quarter < {a1}) AS first, "
        "sum(reports_suspect) FILTER (WHERE quarter >= {b0} AND quarter < {b1}) AS second "
        "FROM {t} WHERE substance_key_a = {k} AND substance_key_b = {k2} "
        "AND ((quarter >= {a0} AND quarter < {a1}) OR (quarter >= {b0} AND quarter < {b1})) "
        "GROUP BY 1"
    ).format(
        t=sql.Identifier(SCHEMA, "faers_pair_quarter"),
        a0=sql.Literal(a0),
        a1=sql.Literal(a1),
        b0=sql.Literal(b0),
        b1=sql.Literal(b1),
        k=sql.Literal(p.moiety_key),
        k2=sql.Literal(p.other_moiety_key),
    )


def v_pair_counts(plan: Any, p: Params) -> list[str]:
    rels = plan_relations(plan)
    raw = [t for t in RAW_FAERS_TABLES if _under(rels, t)]
    out = [f"reads raw FAERS rows ({', '.join(raw)}), not the signal table"] if raw else []
    if not _under(rels, "faers_pair_quarter"):
        out.append("does not read faers_pair_quarter")
    return out


def q_batch_rows(p: Params) -> sql.Composable:
    return sql.SQL(
        "SELECT d.safetyreportid, d.batch_number, d.receive_year FROM {d} d "
        "JOIN {es} es ON es.drug_entry_id = d.drug_entry_id "
        "WHERE es.substance_key = {k} AND d.batch_number IS NOT NULL "
        "AND d.receive_year >= {y0} AND d.receive_year <= {y1}"
    ).format(
        d=sql.Identifier(SCHEMA, "faers_drug"),
        es=sql.Identifier(SCHEMA, "faers_entry_substance"),
        k=sql.Literal(p.moiety_key),
        y0=sql.Literal(p.years[0]),
        y1=sql.Literal(p.years[1]),
    )


def v_batch_rows(plan: Any, p: Params) -> list[str]:
    out = []
    leaves = _under(plan_relations(plan), "faers_drug")
    # One partition per year of the window, and the default partition, which pruning
    # cannot exclude.
    allowed = p.years[1] - p.years[0] + 2
    if len(leaves) > allowed:
        out.append(f"reads {len(leaves)} faers_drug partitions; at most {allowed} expected")
    if not any(i.endswith(("_batched", "_lot")) for i in plan_indexes(plan)):
        out.append("does not use a partial batch index (..._batched or ..._lot)")
    return out


def q_label_interactions(p: Params) -> sql.Composable:
    return sql.SQL(
        "SELECT sec.article_id, sec.start_offset, sec.end_offset FROM {ls} ls "
        "JOIN {sec} sec ON sec.article_id = ls.article_id AND sec.loinc_code = {loinc} "
        "WHERE ls.substance_key = {k}"
    ).format(
        ls=sql.Identifier(SCHEMA, "label_substance"),
        sec=sql.Identifier(SCHEMA, "label_section"),
        loinc=sql.Literal("34073-7"),
        k=sql.Literal(p.moiety_key),
    )


def v_label_interactions(plan: Any, p: Params) -> list[str]:
    rels = set(plan_relations(plan))
    out = []
    if not {"label_substance", "label_section"} <= rels:
        out.append("does not go from label_substance to label_section")
    extra = sorted(r for r in rels if r.startswith(("articles", "article_body")))
    if extra:
        out.append(f"reads the text tables ({', '.join(extra)}); the sections are offsets")
    return out


def q_substance_xref(p: Params) -> sql.Composable:
    return sql.SQL(
        "SELECT x.id_type, x.id_value, x.source FROM {s} s "
        "JOIN {x} x ON x.substance_key = s.substance_key WHERE s.moiety_key = {k}"
    ).format(
        s=sql.Identifier(SCHEMA, "substance"),
        x=sql.Identifier(SCHEMA, "substance_xref"),
        k=sql.Literal(p.moiety_key),
    )


def v_substance_xref(plan: Any, p: Params) -> list[str]:
    out = []
    if not any(i.startswith("substance_xref") for i in plan_indexes(plan)):
        out.append("does not reach substance_xref through an index")
    if not any(i.startswith("substance_moiety_key") for i in plan_indexes(plan)):
        out.append("does not find the moiety's substances through substance_moiety_key_idx")
    return out


def q_recalled_lots(p: Params) -> sql.Composable:
    return sql.SQL(
        "SELECT r.recall_number, r.lot_norm, sum(b.reports) FROM {r} r "
        "JOIN {b} b ON b.substance_key = {k} AND b.batch_norm = r.lot_norm "
        "WHERE r.release_id = {rel} AND r.substance_names && {n}::text[] "
        "GROUP BY 1, 2"
    ).format(
        r=sql.Identifier(SCHEMA, "recall_lot"),
        b=sql.Identifier(SCHEMA, "faers_batch_quarter"),
        k=sql.Literal(p.moiety_key),
        rel=sql.Literal(p.release_id),
        n=sql.Literal(list(p.names)),
    )


def v_recalled_lots(plan: Any, p: Params) -> list[str]:
    rels = plan_relations(plan)
    raw = [t for t in RAW_FAERS_TABLES if _under(rels, t)]
    out = [f"reads raw FAERS rows ({', '.join(raw)}), not faers_batch_quarter"] if raw else []
    if not {"recall_lot", "faers_batch_quarter"} <= set(rels):
        out.append("does not join recall_lot to faers_batch_quarter")
    return out


CHECKS: tuple[Check, ...] = (
    Check("1 latest release of one dataset by substance", q_latest_release, v_latest_release),
    Check("2 FAERS pair counts across two windows", q_pair_counts, v_pair_counts),
    Check("3 FAERS drug rows with a batch number, last two years", q_batch_rows, v_batch_rows),
    Check(
        "4 label interactions section for a substance", q_label_interactions, v_label_interactions
    ),
    Check(
        "5 EU and US view of one moiety through substance_xref", q_substance_xref, v_substance_xref
    ),
    Check("6 recalled lots matched to FAERS batches", q_recalled_lots, v_recalled_lots),
)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------
def _scalar(cur: psycopg.Cursor, query: str, args: tuple = (), default: Any = None) -> Any:
    cur.execute(query, args)
    row = cur.fetchone()
    return default if row is None or row[0] is None else row[0]


def read_params(conn: psycopg.Connection, args: argparse.Namespace) -> Params:
    """The values of the queries, read first and passed as literals. ``latest_release`` is
    read here, not joined in the query."""
    with conn.cursor() as cur:
        release = args.release_id or _scalar(
            cur,
            f"SELECT release_id FROM {SCHEMA}.reg_release WHERE dataset = %s AND is_latest",
            (args.dataset,),
        )
        moiety = args.moiety_key or _scalar(
            cur,
            f"SELECT substance_key FROM {SCHEMA}.faers_moiety_quarter "
            "GROUP BY 1 ORDER BY sum(reports_any) DESC LIMIT 1",
            default=1,
        )
        other = args.other_moiety_key or _scalar(
            cur,
            f"SELECT substance_key_b FROM {SCHEMA}.faers_pair_quarter "
            "WHERE substance_key_a = %s LIMIT 1",
            (moiety,),
            default=moiety,
        )
        newest = _scalar(
            cur, f"SELECT max(quarter) FROM {SCHEMA}.faers_quarter_totals", default=date.today()
        )
        names = _scalar(
            cur,
            f"SELECT substance_names FROM {SCHEMA}.reg_record "
            "WHERE dataset = %s AND release_id = %s AND substance_names IS NOT NULL LIMIT 1",
            (args.dataset, release),
            default=["aspirin"],
        )
    if release is None:
        raise SystemExit(
            f"no latest release for dataset {args.dataset!r}: load it first, or pass --release-id"
        )
    year = newest.year
    return Params(
        dataset=args.dataset,
        release_id=release,
        moiety_key=int(moiety),
        other_moiety_key=int(other),
        names=tuple(names),
        window_a=(date(year - 2, 1, 1), date(year - 1, 1, 1)),
        window_b=(date(year - 1, 1, 1), date(year + 1, 1, 1)),
        years=(year - 1, year),
    )


def explain(conn: psycopg.Connection, query: sql.Composable) -> tuple[Any, str]:
    """(JSON plan, text plan) of ``query``. Neither runs the statement."""
    text = query.as_string(conn)
    with conn.cursor() as cur:
        cur.execute(f"EXPLAIN (FORMAT JSON) {text}")
        plan = cur.fetchone()[0]
        cur.execute(f"EXPLAIN (COSTS OFF) {text}")
        shown = "\n".join(r[0] for r in cur.fetchall())
    return plan, shown


def run_checks(conn: psycopg.Connection, params: Params) -> list[tuple[Check, str, list[str]]]:
    """Run every check inside the read-only transaction of ``conn``; roll back at the end."""
    results = []
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL enable_seqscan = off")
        for check in CHECKS:
            plan, shown = explain(conn, check.build(params))
            results.append((check, shown, check.verify(plan, params)))
    finally:
        conn.rollback()
    return results


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--dataset", default="fda_ndc", help="reg_record dataset (default: fda_ndc)")
    ap.add_argument("--release-id", default=None, help="default: the latest release, read first")
    ap.add_argument("--moiety-key", type=int, default=None, help="default: the busiest in FAERS")
    ap.add_argument("--other-moiety-key", type=int, default=None)
    return ap


def main(argv: list[str] | None = None) -> int:
    from episteme.config import get_settings
    from episteme.data.db.connection import connect_kwargs, dsn_from_settings

    args = build_parser().parse_args(argv)
    settings = get_settings()
    # dsn_from_settings runs the DB-mode guard; the connection itself is read only.
    with psycopg.connect(dsn_from_settings(), **connect_kwargs(settings)) as conn:
        conn.read_only = True
        params = read_params(conn, args)
        conn.rollback()
        results = run_checks(conn, params)
    failed = 0
    for check, shown, problems in results:
        print(f"== {check.name}: {'FAIL' if problems else 'ok'}")
        print(shown)
        for problem in problems:
            print(f"   - {problem}")
        failed += bool(problems)
    print(f"{len(results) - failed} of {len(results)} plans as expected")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
