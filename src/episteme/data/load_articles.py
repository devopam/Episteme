"""CLI: walk a source's staging shards and load each into Postgres.

    python -m episteme.data.load_articles --source pmc [--processed-dir DIR] \\
        [--force --reason "..."] [--workers N] [--only NAME ...]

One pooled connection for the whole run. ``run_start`` / ``run_end`` audit
events bracket the run; each shard is its own committed transaction via
``postgres_loader.load_source_file`` + ``conn.commit()``. A shard whose
``load_success`` marker already exists is skipped unless ``--force`` (which
requires ``--reason`` and emits a ``force_override`` audit event).

``--only NAME`` (repeatable, generic, backwards-compatible -- default is no
filtering) restricts the run to shards whose file name is in the given set;
every other discovered shard is neither loaded nor reported as failed, as if
it were never discovered. Added for SP7's cdisc_ct (I1, whole-branch review
Ruling FW7-1): ``load_cdisc_ct.sh`` computes each package's newest,
already-serialized shard name via
``python -m episteme.data.cdisc_ct.retire --print-current-shards`` and
forwards those names here, so an older release's shard sitting in the same
staging directory (left over from before the newest release was
discovered/serialized) can never load after -- or instead of -- the newest
one, even under ``--force`` or after a transient retry.

Config comes from ``episteme.config`` -- this module never reads ``os.environ``.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from episteme import audit_trail
from episteme.config import get_settings
from episteme.data import checkpoint_markers, postgres_loader
from episteme.data.article_schema import SCHEMA_VERSION, utc_now_iso
from episteme.data.db.connection import connection
from episteme.ops import orphans
from episteme.ops.failure import record_failure
from episteme.ops.retry import RETRY_WAITS, with_retries
from episteme.ops.skip import add_skip_args, apply_skips, check_skip_args
from episteme.ops.status import current
from episteme.ops.stop import stage_guard

_LOG = logging.getLogger(__name__)
_SLEEP = time.sleep  # seam: tests patch this so retry waits cost nothing


def _write_load_success(processed_root: Path, source: str, basename: str, stats: dict) -> Path:
    """Write the per-shard ``load_success`` marker.

    ``checkpoint_markers`` (a Task-3 deliverable, not to be restructured) ships
    ``load_success_marker_path`` but no writer for it -- only ``mark_success``,
    which targets the *extract* stage's ``success/`` namespace. Writing there
    would make the skip check (which reads ``load_success/``) dead code, so the
    marker is written inline here with the same payload shape ``mark_success``
    uses. Deviation from the brief's "call mark_success" -- see task report.
    """
    path = checkpoint_markers.load_success_marker_path(processed_root, source, basename)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "source_file": basename,
        "completed_at": utc_now_iso(),
        "stats": stats or {},
    }
    return checkpoint_markers.write_marker(path, payload)


def _discover_shards(staging_dir: Path) -> list[Path]:
    return sorted(staging_dir.glob("*.parquet")) + sorted(staging_dir.glob("*.jsonl"))


def _filter_only(shards: list[Path], only: list[str] | None) -> list[Path]:
    """``--only`` filter, factored out for direct testing (I1). ``only`` is
    ``None``/empty -> no filtering (backwards compatible); otherwise keep only
    the shards whose ``.name`` is in ``only`` -- everything else is dropped as
    if it had never been discovered (not loaded, not reported as failed)."""
    if not only:
        return shards
    keep = set(only)
    return [s for s in shards if s.name in keep]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m episteme.data.load_articles",
        description="Load staging shards for one source into episteme.articles.",
    )
    parser.add_argument("--source", required=True, help="source name (SP1-beta: pmc)")
    parser.add_argument(
        "--processed-dir",
        type=Path,
        default=get_settings().processed_root,
        help="processed root holding staging/<source>/*.parquet|*.jsonl",
    )
    parser.add_argument(
        "--force", action="store_true", help="reload shards that have a load_success marker"
    )
    parser.add_argument("--reason", type=str, default=None, help="required with --force (audited)")
    parser.add_argument("--workers", type=int, default=1, help="reserved; loading stays sequential")
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="NAME",
        help=(
            "load only the shard(s) whose file name is NAME (repeatable); "
            "any other discovered shard is neither loaded nor reported as failed"
        ),
    )
    add_skip_args(parser)  # --skip-unit; --reason is shared with --force
    args = parser.parse_args(argv)
    check_skip_args(parser, args)

    source: str = args.source
    processed_dir = Path(args.processed_dir)

    if args.force and not args.reason:
        print("error: --force requires --reason", file=sys.stderr)
        return 2

    staging_dir = processed_dir / "staging" / source
    shards = _filter_only(_discover_shards(staging_dir), args.only)
    if not shards and args.only:
        # The caller named specific shards and none exist: a caller bug (e.g. a
        # stray \r in the name), not an empty source -- fail loudly.
        print(
            f"error: none of the --only shard(s) {sorted(args.only)!r} exist under {staging_dir}",
            file=sys.stderr,
        )
        return 1
    if args.skip_unit:
        unknown = sorted(set(args.skip_unit) - {sh.name for sh in shards})
        if unknown:
            print(
                f"error: --skip-unit name(s) {unknown!r} match no load unit of {source}",
                file=sys.stderr,
            )
            return 2

    if not shards:
        print(f"no shards under {staging_dir}")
        return 0

    external_run_id = get_settings().run_id
    run_id = external_run_id or f"{source}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    # Skip our own run_start/run_end bracket when an orchestrator (e.g.
    # run_pipeline.sh, via EPISTEME_RUN_ID) already supplied the run_id --
    # otherwise a consumer sees two nested brackets for one logical run.
    own_bracket = external_run_id is None

    if args.workers > 1:
        # TODO(SP3): parallel via ProcessPoolExecutor (process_one + --executor).
        # SP1-β exit criteria only require correctness, so loading is sequential.
        print(f"note: --workers {args.workers} ignored; loading sequentially (SP1-β)")

    holder: list = [None]  # the connection of the attempt in progress, for stage_guard

    def _bracket(event: str, reason: str | None = None) -> None:
        with connection() as bconn:
            audit_trail.record(event, conn=bconn, object=source, run_id=run_id, reason=reason)
            bconn.commit()

    def _load_one(shard: Path, name: str) -> dict:
        """One attempt: a connection from the pool, one transaction, one commit."""
        with connection() as conn:
            holder[0] = conn
            current().update(unit=name, backend_pid=conn.info.backend_pid)
            try:
                with audit_trail.deferred_mirror(conn):
                    try:
                        if args.force and args.reason:
                            audit_trail.record(
                                "force_override",
                                conn=conn,
                                object=f"{source} {name}",
                                reason=args.reason,
                                run_id=run_id,
                            )
                        result = postgres_loader.load_source_file(
                            conn, source=source, staging_path=shard, run_id=run_id
                        )
                        audit_trail.commit(conn)
                        return result
                    except BaseException as exc:
                        if not isinstance(exc, Exception):
                            # Stop (KeyboardInterrupt/SystemExit): cancel the running
                            # statement explicitly. psycopg also interrupts it on
                            # KeyboardInterrupt, but do not rely on that alone.
                            try:
                                conn.cancel_safe()
                            except Exception:  # noqa: BLE001 - best effort
                                pass
                        try:
                            audit_trail.rollback(conn)
                        except Exception:  # noqa: BLE001 - keep the original error
                            pass
                        raise
            finally:
                holder[0] = None

    def _on_retry(name: str):
        def _cb(attempt: int, exc: BaseException, wait: float) -> None:
            print(f"retry {name} ({attempt}/{len(RETRY_WAITS)}) in {wait}s: {exc!r}")

        return _cb

    run_ended = False

    def _end_bracket(reason: str | None = None) -> None:
        nonlocal run_ended
        if not own_bracket or run_ended:
            return
        run_ended = True  # never two run_end rows, even if the write fails
        _bracket("run_end", reason=reason)

    failed_at: str | None = None
    skipped_by_operator = 0
    with stage_guard(lambda: holder[0], lock_stage="load", db_lock=True) as g:
        if own_bracket:
            _bracket("run_start")
        try:

            def _sweep() -> None:
                with connection() as sconn:
                    orphans.sweep(sconn)

            # The start-up connection gets the same retry schedule as a unit.
            with_retries(_sweep, sleep=lambda secs: _SLEEP(secs), on_retry=_on_retry("sweep"))

            keep = set(
                apply_skips(
                    source,
                    "load",
                    [s.name for s in shards],
                    args.skip_unit,
                    args.reason,
                    run_id=run_id,
                )
            )
            skipped_by_operator = len(shards) - len(keep)

            shards_done = 0
            for shard in shards:
                name = shard.name
                if name not in keep:
                    print(f"skip-unit {name}")
                    shards_done += 1
                    current().progress(unit=name, units_done=shards_done, units_total=len(shards))
                    continue
                marker = checkpoint_markers.load_success_marker_path(processed_dir, source, name)
                if marker.is_file() and not args.force:
                    print(f"skip {name}")
                    shards_done += 1
                    current().progress(unit=name, units_done=shards_done, units_total=len(shards))
                    continue

                try:
                    result = with_retries(
                        lambda shard=shard, name=name: _load_one(shard, name),
                        sleep=lambda secs: _SLEEP(secs),
                        on_retry=_on_retry(name),
                    )
                except Exception as exc:  # noqa: BLE001 - permanent: record, stop the run
                    failed_at = name
                    print(f"FAIL {name}: {exc}")
                    record_failure(source, "load", name, repr(exc), run_id=run_id)
                    _end_bracket(f"failed at shard {name}: {exc!r}"[:500])
                    break

                postgres_loader.add_committed_rows(result["rows"])  # committed by _load_one
                _write_load_success(processed_dir, source, name, result)
                shards_done += 1
                current().progress(unit=name, units_done=shards_done, units_total=len(shards))
                print(
                    f"ok {name} rows={result['rows']} deleted={result['deleted']} "
                    f"body_deleted={result['body_deleted']} event={result['event']} "
                    f"read_sec={result['read_sec']} copy_sec={result['copy_sec']} "
                    f"rows_per_sec={result['rows_per_sec']}"
                )
            _end_bracket()
        except BaseException as exc:
            # Every other non-success exit (stop, sweep error, unexpected error)
            # closes the bracket once; a failure here must not hide `exc`.
            reason = (
                f"stopped ({type(exc).__name__})"
                if not isinstance(exc, Exception)
                else f"failed: {exc!r}"[:500]
            )
            try:
                _end_bracket(reason)
            except Exception:  # noqa: BLE001 - keep the original exception
                _LOG.exception("could not write run_end after %r", exc)
            raise

        if failed_at is not None:
            print(f"done shards={len(shards)} failed=1 stopped_at={failed_at}")
            return g.exit_code(1)
        extra = f" skipped_by_operator={skipped_by_operator}" if skipped_by_operator else ""
        print(f"done shards={len(shards)} failed=0{extra}")
        return g.exit_code(0)


if __name__ == "__main__":
    raise SystemExit(main())
