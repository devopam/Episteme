"""CLI: retire the rows of deleted pubchem parts from episteme.articles /
episteme.article_body after a good load.

    python -m episteme.data.pubchem.retire [--processed-dir ...]

Why: ``serialize`` writes one unit (a ``CID-SMILES`` primary file) as parts
``<input_key>__partNNNNN``. A rebuild (``--force``, a changed input) or a
resume can delete parts. Their shards and markers go, but their rows stay in
the database, because a plain ``load`` replaces only the ``source_file``
values it loads.

How: when ``serialize`` deletes a part that has a ``load_success`` marker (it
was loaded), it first adds the part name to
``_ops/pubchem/retire_pending.json`` (``serialize_pubchem._delete_parts_above``).
A part deleted without a load marker was never loaded and is not recorded.
This step reads that file:

1. No file, or an empty list: nothing to do. No lock, no database query.
2. Else it takes the heavy lock of the ``load`` stage (serialize writes the
   file under the same lock), reads the file again, and classifies each name
   (``classify_pending``): no part success marker -> delete its rows; a
   success marker and a load_success marker (a later serialize wrote the part
   again and it was loaded) -> drop the name; a success marker only (written
   again, not loaded yet, e.g. ``--skip-unit``) -> keep the name pending and
   delete nothing.
3. With names to delete it also takes the advisory lock, and one
   ``postgres_loader.delete_source_files`` call deletes exactly their
   ``pubchem`` rows (``source_file = ANY(...)``, index-only), with one
   ``load_replace`` audit row that names them in ``old_value``, and commits.
4. Still under the heavy lock, it writes back the file without the names it
   deleted or dropped.

A stop between the commit and the file update leaves the names in the file;
the next run deletes them again (0 rows, one more audit row): idempotent.
A file that is not a JSON list of strings exits 1 before any database call.
The connection of the running delete is visible to the stop handler, so a
stop or the parent watchdog cancels it.

``load_pubchem.sh`` runs this only after ``load_articles`` exited 0.
``--force``, ``--reason`` and ``--skip-unit`` are accepted and ignored:
``load_pubchem.sh`` forwards run_pipeline.sh's ``load_args`` to this script.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from episteme.config import get_settings
from episteme.data import checkpoint_markers, postgres_loader
from episteme.data.pubchem.serialize_pubchem import (
    SOURCE,
    read_retire_pending,
    write_retire_pending,
)

_SHARD_EXTS: tuple[str, ...] = (".parquet", ".jsonl")


def _is_loaded(processed_dir: Path, name: str) -> bool:
    """True when the shard of part ``name`` has a load_success marker (the
    loader names it by shard file name: ``.parquet`` or ``.jsonl``)."""
    return any(
        checkpoint_markers.load_success_marker_path(processed_dir, SOURCE, f"{name}{ext}").is_file()
        for ext in _SHARD_EXTS
    )


def classify_pending(
    processed_dir: Path, pending: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """``(to_delete, done, waiting)`` for the pending part names:

    - no part success marker: the part is gone; delete its rows;
    - a success marker AND a load_success marker: a later serialize wrote the
      part again and it was loaded (the loader replaced its rows by
      source_file); drop the name;
    - a success marker only: the part was written again but not loaded yet
      (for example ``--skip-unit``). Its old rows may hold CIDs that the new
      part does not, so keep the name pending and delete nothing."""
    to_delete: list[str] = []
    done: list[str] = []
    waiting: list[str] = []
    for name in pending:
        if not checkpoint_markers.success_marker_path(processed_dir, SOURCE, name).is_file():
            to_delete.append(name)
        elif _is_loaded(processed_dir, name):
            done.append(name)
        else:
            waiting.append(name)
    return to_delete, done, waiting


def _retire(processed_dir: Path, run_id: str | None) -> int:
    # A first read only decides whether a lock is needed at all.
    try:
        pending = read_retire_pending(processed_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not pending:
        print("pubchem: no deleted part pending; nothing retired")
        return 0

    from episteme.data.db.connection import connection
    from episteme.ops import lock
    from episteme.ops import status as ops_status
    from episteme.ops.stop import stage_guard

    holder: list = [None]  # the connection of the running delete, for stage_guard
    # The heavy lock covers the pending file: serialize writes it under the same
    # lock, so the file is read again, classified and written back inside it.
    with stage_guard(lambda: holder[0], lock_stage="load", source=SOURCE) as g:
        try:
            pending = read_retire_pending(processed_dir)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return g.exit_code(1)
        to_delete, done, waiting = classify_pending(processed_dir, pending)
        for name in done:
            print(f"{name}: written again and loaded -- dropped")
        for name in waiting:
            print(f"{name}: written again, not loaded yet -- stays pending")
        deleted = 0
        if to_delete:
            # The advisory lock only when there is a delete to run.
            with (
                ops_status.current().long_statement("waiting for the advisory lock"),
                lock.advisory_lock(wait=not lock.no_wait()),
            ):
                with connection() as conn:
                    holder[0] = conn
                    try:
                        deleted = postgres_loader.delete_source_files(
                            conn, source=SOURCE, source_files=to_delete, run_id=run_id
                        )
                        conn.commit()
                    finally:
                        holder[0] = None
        removed = set(to_delete) | set(done)
        if removed:
            # After the commit: a stop before this write leaves the names
            # pending, and the next run deletes them again (0 rows). Only the
            # names this run deleted or dropped leave the file.
            current_pending = read_retire_pending(processed_dir)
            write_retire_pending(processed_dir, [n for n in current_pending if n not in removed])
        print(
            f"pubchem: retired {deleted} article(s) of {len(to_delete)} deleted part(s); "
            f"{len(waiting)} part(s) stay pending"
        )
        return g.exit_code(0)


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    p = argparse.ArgumentParser(
        prog="python -m episteme.data.pubchem.retire",
        description="Retire the rows of deleted pubchem parts from episteme.articles/article_body.",
    )
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument("--force", action="store_true", help="accepted, unused (see module docstring)")
    p.add_argument("--reason", default=None, help="accepted, unused (see module docstring)")
    p.add_argument(
        "--skip-unit",
        action="append",
        default=None,
        metavar="NAME",
        help="accepted, unused (see module docstring)",
    )
    args = p.parse_args(argv)
    return _retire(args.processed_dir, settings.run_id)


if __name__ == "__main__":
    raise SystemExit(main())
