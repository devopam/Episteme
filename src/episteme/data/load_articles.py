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
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from episteme import audit_trail
from episteme.config import get_settings
from episteme.data import checkpoint_markers, postgres_loader
from episteme.data.article_schema import SCHEMA_VERSION, utc_now_iso
from episteme.data.db.connection import connection


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
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


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
    args = parser.parse_args(argv)

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

    failures = 0
    with connection() as conn:
        if own_bracket:
            audit_trail.record("run_start", conn=conn, object=source, run_id=run_id)
            conn.commit()

        for shard in shards:
            name = shard.name
            marker = checkpoint_markers.load_success_marker_path(processed_dir, source, name)
            if marker.is_file() and not args.force:
                print(f"skip {name}")
                continue

            if args.force and args.reason:
                audit_trail.record(
                    "force_override",
                    conn=conn,
                    object=f"{source} {name}",
                    reason=args.reason,
                    run_id=run_id,
                )

            try:
                result = postgres_loader.load_source_file(
                    conn, source=source, staging_path=shard, run_id=run_id
                )
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - report + continue to next shard
                conn.rollback()
                failures += 1
                print(f"FAIL {name}: {exc}")
                continue

            _write_load_success(processed_dir, source, name, result)
            print(
                f"ok {name} rows={result['rows']} deleted={result['deleted']} "
                f"body_deleted={result['body_deleted']} event={result['event']}"
            )

        if own_bracket:
            audit_trail.record("run_end", conn=conn, object=source, run_id=run_id)
            conn.commit()

    print(f"done shards={len(shards)} failed={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
