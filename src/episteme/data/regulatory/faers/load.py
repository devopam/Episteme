"""Load FAERS by generation: readers never see a half-replaced state.

    python -m episteme.data.regulatory.faers.load [--raw-dir DIR] [--force --reason "..."]
        [--allow-partial --reason "..."]

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
   the same across partitions, for copies that disagree on the first receipt
   (``duplicates_cross_year``). That pass is not bounded by a partition: its ``GROUP BY``
   reads all ids of the generation. No index exists yet, so any pass that finds losers
   reads the whole of its child tables to delete them.
4. **Indexes** per partition, one transaction per partition, then ``ANALYZE`` of each
   parent (which also collects the parent-level statistics that autovacuum never does).
5. **``post_load(conn)``,** when given (the CLI gives ``build_signals``), then the **swap**:
   one transaction under ``lock_timeout`` (``EPISTEME_FAERS_LOCK_TIMEOUT_S``) renames the
   live parents and their partitions to ``<parent>_old_<gen>`` and
   ``<parent>_old_<gen>_y2024``, renames the new ones to the plain names of migration 0010
   (``faers_drug``, ``faers_drug_y2024``), and drops the progress table. The signal tables
   built under ``<gen>`` swap in the same transaction, so a reader never sees new rows with
   old signals; with no finished build, the live ones are emptied. A later transaction drops
   the old generation and its signal tables. If a reader
   holds a lock, the swap fails with ``GenerationSwapError``, the old generation keeps
   serving, and the built generation stays for the next run. The index names keep their
   ``<gen>`` token after the swap (``faers_drug_<gen>_y2024_sid``); the token is new for
   each generation, so index names never collide.

**Refusals.** A swap replaces everything, and a report absent from the inputs disappears.
So a partial or stale input set must not go live:

* ``check_input_set``, on file names, before any work: each quarter must hold exactly
  ``1..N of N``, with one ``N``, and the quarter folders must be continuous from the
  earliest present to the latest present. A download that stopped, ``--max-files``, a
  stale ``-of-0009`` next to a new ``-of-0010``, and a quarter missing in between all fail
  it. A quarter missing at either end does not, so:
* ``below_floor``, before the indexes: a new generation below
  ``EPISTEME_FAERS_MIN_LIVE_SHARE`` of the live report count stops. With no live
  generation there is no floor. The built generation stays, so a re-run with the override
  goes on from there.

* With ``check_signals`` (the CLI always sets it): ``substance_empty``, before any work, refuses
  a database with no ``substance`` row (the signals would be empty), and ``signal_problems``,
  before the swap, refuses signals whose bridge tied no drug row of the generation or whose
  ``faers_pair_quarter`` would have no row. A load into ``episteme_test`` must never leave
  production with empty signals.

``override_reason`` (CLI: ``--allow-partial --reason``) passes all of them, and writes one
``force_override`` audit row with its reason for each check that it passes. ``--force``
alone does not. A sample run (``sample``; the CLI sets it on the secondary database, which
only ``run_pipeline.sh --sample`` selects) reads a slice on purpose and skips the first two;
it does not skip the substance and signal checks.

**Resume: how a run decides** (``decide_generation``). The fingerprint is a SHA-256 of
``FORMAT_VERSION`` (of ``parse``) and the ``input_key``, size and modification time of every
input, so a change of the parser's rules can neither resume nor pass as current. It is
stored as the table comment of the generation's ``faers_report`` parent, so it stays with
the table through the swap.

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

**Modules:** ``generation`` holds the names, the decision, the catalog, de-duplication,
indexes and the swap; ``zip_load`` the COPY and the dictionaries; ``signals`` the bridge and
the signal tables.

**For ``post_load``:** it runs after the indexes and before the swap. A success is recorded,
with the dict it returns (``post_load`` in the result and in the audit row), so a re-run does
not repeat it; a failure is run again by the next run, so it must clean up after itself. The
plain names still hold the old generation at that step, so it finds
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
import functools
import hashlib
import json
import logging
import sys
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path

import ijson
import psycopg
from psycopg.pq import TransactionStatus

from episteme import audit_trail
from episteme.config import get_settings
from episteme.data import checkpoint_markers
from episteme.data.regulatory.faers.generation import (
    GENERATION_RE,
    INDEXES,
    PARENTS,
    SOURCE,
    Decision,
    GenerationError,
    GenerationSwapError,
    InputSetError,
    _audit_override,
    _build_indexes,
    _check_access,
    _check_rows,
    _comment,
    _create_generation,
    _dedupe,
    _drop_generation,
    _drop_old_generation,
    _drop_old_leftovers,
    _live_reports,
    _put_state,
    _read_state,
    _sum_stats,
    _swap,
    below_floor,
    check_input_set,
    decide_generation,
    faers_zips,
    generation_table,
    leaf_table,
    new_generation_id,
    old_leaf_table,
    old_table,
    parse_bound,
    state_table,
    unfinished_generations,
)
from episteme.data.regulatory.faers.parse import FORMAT_VERSION
from episteme.data.regulatory.faers.signals import bridge_entries, rebuild_signals
from episteme.data.regulatory.faers.zip_load import Batch, _load_zip, _write_zip_marker
from episteme.data.regulatory.load_reg import input_keys, latest_release
from episteme.data.regulatory.recall_lots import RECALL_DATASET, build_recall_lots
from episteme.ops.failure import record_failure

_LOG = logging.getLogger(__name__)

# The names of this package's load stage, whichever module holds them.
__all__ = [
    "GENERATION_RE",
    "INDEXES",
    "PARENTS",
    "SOURCE",
    "Batch",
    "Decision",
    "GenerationError",
    "GenerationSwapError",
    "InputSetError",
    "below_floor",
    "build_signals",
    "check_input_set",
    "checkpoint_markers",
    "decide_generation",
    "faers_zips",
    "fingerprint",
    "generation_table",
    "input_keys",
    "leaf_table",
    "load_generation",
    "main",
    "new_generation_id",
    "old_leaf_table",
    "old_table",
    "parse_bound",
    "signal_problems",
    "state_table",
    "substance_empty",
    "unfinished_generations",
]


def fingerprint(files: Iterable[tuple[Path, str]]) -> str:
    """SHA-256 over ``FORMAT_VERSION`` and ``(input_key, size, mtime_ns)`` of the inputs,
    in key order."""
    rows = []
    for path, key in files:
        st = Path(path).stat()
        rows.append([key, st.st_size, st.st_mtime_ns])
    return hashlib.sha256(json.dumps([FORMAT_VERSION, sorted(rows)]).encode("utf-8")).hexdigest()


def build_signals(conn: psycopg.Connection, *, redo: bool = False) -> dict:
    """The ``post_load`` of the CLI: bridge new entries (with ``redo``, all entries again, so
    a corrected xref link reaches the signals; the CLI sets it with ``--force``), then build
    the signal tables of the one unfinished generation. They swap with it."""
    generation = next(iter(unfinished_generations(conn)))
    conn.commit()
    state = _read_state(conn, generation)
    bridge = state.get(("phase", "bridged"))
    if bridge is None:  # a re-run keeps the map that its finished partitions were built on
        bridge = bridge_entries(conn, redo=redo)
        with conn.cursor() as cur:
            _put_state(cur, generation, "phase", "bridged", bridge)
        conn.commit()
    signals = rebuild_signals(
        conn, pair_drug_cap=get_settings().faers_pair_drug_cap, generation=generation
    )
    return {"bridge": bridge, "signals": signals}


def substance_empty(conn: psycopg.Connection) -> bool:
    """Whether ``substance`` has no row (the FDA substance data is not loaded in this
    database). The read is rolled back."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT NOT EXISTS (SELECT 1 FROM episteme.substance)")
            return bool(cur.fetchone()[0])
    finally:
        conn.rollback()


def signal_problems(post_load_stats: dict) -> list[str]:
    """Why the signals of a built generation must not swap in: the bridge tied no drug row
    of the generation to a substance, or ``faers_pair_quarter`` would have no row. Reads the
    counts that ``build_signals`` returned (kept in the progress table, so a resumed run is
    checked as well). Empty when the signals are usable."""
    signals = post_load_stats.get("signals") or {}
    out = []
    bridged = int(signals.get("drug_rows") or 0) - int(signals.get("drug_rows_unbridged") or 0)
    if bridged <= 0:
        out.append(
            "the bridge matched no drug entry of this generation to a substance "
            f"(drug_rows {signals.get('drug_rows', 0)}, all unbridged)"
        )
    if int(signals.get("pair_rows") or 0) <= 0:
        out.append("faers_pair_quarter would have 0 rows")
    return out


def signal_refusal_message(issues: list[str], generation: str) -> str:
    """The refusal text for signals that must not swap in. It names the recovery step
    (``--force --reason`` starts a new generation and bridges every entry again) and the
    override (``--allow-partial --reason``) for a result that is right."""
    return (
        "; ".join(issues) + f"; no swap. Generation {generation} stays built. Check that the "
        "substance data is loaded in this database, then run the load again with "
        '--force --reason "<why>" (a new generation, every entry bridged again). If the '
        "result is right, run it with --allow-partial --reason."
    )


def load_generation(
    conn: psycopg.Connection,
    zips: Sequence[Path],
    *,
    raw_dir: Path,
    processed_dir: Path,
    run_id: str,
    post_load: Callable[[psycopg.Connection], dict | None] | None = None,
    force: bool = False,
    override_reason: str | None = None,
    sample: bool = False,
    check_signals: bool = False,
) -> dict:
    """Build a generation from ``zips`` and swap it in; see the module docstring.

    ``check_signals`` (the CLI sets it, with ``build_signals`` as ``post_load``) turns on
    the empty-substance and empty-signal refusals; it needs a ``post_load`` that returns the
    counts of ``build_signals``.

    Commits on ``conn``, which must be idle. Returns ``status`` (``swapped`` or
    ``current``), ``generation``, ``zips_loaded``, ``zips_skipped``,
    ``old_generation_dropped`` and, when swapped: ``reports_read``, ``drug_rows_read``,
    ``reaction_rows_read`` (after the id checks of each zip), ``reports``, ``drug_rows``,
    ``reaction_rows`` (after de-duplication), ``no_date``, ``no_id``, ``long_ids``,
    ``long_terms``, ``long_batches``, ``overrides`` (the checks ``override_reason``
    passed), ``duplicates_in_zip``, ``duplicates_removed`` (all rows of ``faers_report`` removed),
    ``duplicates_cross_year`` (those removed across partitions), ``reports_in_default``,
    ``reports_no_date``.

    Raises on a failure, and the old generation keeps serving: ``InputSetError`` for a
    partial input set, a zip that does not parse, a failed ``post_load``,
    ``GenerationError`` from a check, ``GenerationSwapError`` from the swap's lock.
    """
    paths = [Path(p) for p in zips]
    if not paths:
        raise ValueError("load_generation needs at least one input file")
    files = sorted(zip(input_keys(paths, raw_dir), paths, strict=True))
    problems = [] if sample else check_input_set(paths)
    if problems and override_reason is None:
        raise InputSetError(
            "the input files are not a whole issue: "
            + "; ".join(problems)
            + ". Finish the download and remove stale files, or pass --allow-partial --reason."
        )
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise GenerationError("load_generation owns the transactions: the connection is not idle")
    inputs_fp = fingerprint((p, k) for k, p in files)
    _LOG.info("ijson backend: %s; %d zips", ijson.backend, len(files))
    overrides: list[str] = []
    if problems:
        _audit_override(conn, "input set", override_reason, {"problems": problems}, run_id)
        overrides.append("input set")
    # The signals need substances. A sample run does not skip this: an empty substance
    # table in the target database would swap in empty signals.
    if check_signals and substance_empty(conn):
        if override_reason is None:
            raise GenerationError(
                "episteme.substance is empty in this database: load the FDA substance data "
                "first (scripts/data/openfda/load_substance.sh), in this database; nothing "
                "was loaded. To go on anyway, pass --allow-partial --reason."
            )
        _audit_override(conn, "empty substance", override_reason, {"substances": 0}, run_id)
        overrides.append("empty substance")

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
    live_reports = _live_reports(conn)
    share = get_settings().faers_min_live_share
    if not sample and below_floor(checked["reports"], live_reports, share):
        detail = {"reports": checked["reports"], "live_reports": live_reports, "share": str(share)}
        if override_reason is None:
            raise GenerationError(
                f"the new generation has {checked['reports']} reports, below {share} "
                f"(EPISTEME_FAERS_MIN_LIVE_SHARE) of the {live_reports} live reports; no swap. "
                f"Generation {generation} stays built: if this is right, run the load again "
                "with --allow-partial --reason."
            )
        _audit_override(conn, "live floor", override_reason, detail, run_id)
        overrides.append("live floor")

    if ("phase", "indexed") not in state:
        _build_indexes(conn, generation)
    if post_load is not None and ("phase", "post_load") not in state:
        try:
            stats = post_load(conn) or {}
            with conn.cursor() as cur:
                _put_state(cur, generation, "phase", "post_load", stats)
            conn.commit()
            state["phase", "post_load"] = stats
        except BaseException:
            conn.rollback()
            raise
    if check_signals:
        # Not bypassed by a sample run: empty signals must never swap in silently.
        signal_issues = signal_problems(state.get(("phase", "post_load")) or {})
        if signal_issues:
            if override_reason is None:
                raise GenerationError(signal_refusal_message(signal_issues, generation))
            _audit_override(
                conn, "empty signals", override_reason, {"problems": signal_issues}, run_id
            )
            overrides.append("empty signals")

    audit_counts = {
        "reports": checked["reports"],
        "reports_in_default": checked["reports_in_default"],
        "reports_no_date": checked["reports_no_date"],
        "no_date": read["no_date"],
        "no_id": read["no_id"],
        "long_ids": read["long_ids"],
        "long_terms": read["long_terms"],
        "long_batches": read["long_batches"],
        "duplicates_in_zip": read["duplicates_in_zip"],
        "duplicates_removed": removed["reports"],
        "duplicates_cross_year": cross["reports"],
        "post_load": state.get(("phase", "post_load"), {}),
    }
    live_state = {"fingerprint": live, "reports": live_reports}
    _swap(conn, generation, inputs_fp, audit_counts, live_state, run_id)
    dropped = _drop_old_generation(conn, generation)
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
        "overrides": overrides,
        **audit_counts,
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
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="go on with a partial input set or below the live floor",
    )
    parser.add_argument(
        "--reason", default=None, help="required with --force and --allow-partial (audited)"
    )
    args = parser.parse_args(argv)
    if args.force and not args.reason:
        print("error: --force requires --reason", file=sys.stderr)
        return 2
    if args.allow_partial and not args.reason:
        print("error: --allow-partial requires --reason", file=sys.stderr)
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
                        override_reason=args.reason if args.allow_partial else None,
                        # Only run_pipeline.sh --sample selects the secondary database.
                        sample=settings.db_target == "secondary",
                        # --force re-bridges every entry, so a corrected link reaches FAERS.
                        post_load=functools.partial(build_signals, redo=args.force),
                        check_signals=True,
                    )
                    print("done " + " ".join(f"{k}={v}" for k, v in result.items()))
                    # Not part of the generation, and quick, so it runs on a "current" too.
                    release = latest_release(conn, RECALL_DATASET)
                    if release is None:
                        conn.rollback()
                        print(f"no latest {RECALL_DATASET} release: recall_lot not built")
                    else:
                        try:
                            with audit_trail.deferred_mirror(conn):
                                lots = build_recall_lots(conn, release, run_id=run_id)
                                audit_trail.commit(conn)
                        except BaseException:
                            audit_trail.rollback(conn)
                            raise
                        print("recall_lot " + " ".join(f"{k}={v}" for k, v in lots.items()))
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
