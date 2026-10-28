"""Shared retire path for sources that load bounded parts (pubchem, pmc): retire
the rows of deleted parts from episteme.articles / episteme.article_body after a
good load. One module, parameterised by ``source``; the per-source entry points
(``pubchem.retire``, ``pmc.retire``) are thin.

    python -m episteme.data.<source>.retire [--processed-dir ...]

Why: ``serialize`` writes one unit (a ``CID-SMILES`` primary file) as parts
``<input_key>__partNNNNN``. A rebuild (``--force``, a changed input) or a
resume can delete parts. Their shards and markers go, but their rows stay in
the database, because a plain ``load`` replaces only the ``source_file``
values it loads.

How: when ``serialize`` deletes a part that has a ``load_success`` marker (it
was loaded), it first adds the part name to
``_ops/<source>/retire_pending.json`` (the serializer's ``_delete_parts_above``).
A part deleted without a load marker was never loaded and is not recorded.
This step reads that file:

1. No file, or an empty list: nothing to do. No lock, no database query.
2. Else it takes the heavy lock of the ``load`` stage (serialize writes the
   file under the same lock), reads the file again, and classifies each name
   (``classify_pending``, the load marker first): a load_success marker (a
   load replaced the rows of the name after it was recorded, with or without
   a part success marker) -> drop the name; a success marker only (written
   again, not loaded yet, e.g. ``--skip-unit``) -> keep the name pending and
   delete nothing; neither -> delete its rows.
3. With names to delete it also takes the advisory lock, and one
   ``postgres_loader.delete_source_files`` call deletes exactly their
   rows of the source (``source_file = ANY(...)``, index-only), with one
   ``load_replace`` audit row that names them in ``old_value``, and commits.
4. Still under the heavy lock, it writes back the file without the names it
   deleted or dropped.

A stop between the commit and the file update leaves the names in the file;
the next run deletes them again (0 rows, one more audit row): idempotent.
A file that is not a JSON list of strings exits 1 before any database call.
The connection of the running delete is visible to the stop handler, so a
stop or the parent watchdog cancels it.

``load_<source>.sh`` runs this only after ``load_articles`` exited 0.
``--force``, ``--reason`` and ``--skip-unit`` are accepted and ignored:
``load_<source>.sh`` forwards run_pipeline.sh's ``load_args`` to this script.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from episteme.config import get_settings
from episteme.data import checkpoint_markers, postgres_loader
from episteme.data.checkpoint_markers import ops_root, write_marker

_SHARD_EXTS: tuple[str, ...] = (".parquet", ".jsonl")

# The retire pending file: the part names (source_file values) whose parts the
# serializer deleted after they were loaded. A JSON list, sorted, no
# duplicates, written atomically. The retire step reads it after a good load.
_PENDING_LOCK = threading.Lock()


def retire_pending_path(processed_dir: Path, source: str) -> Path:
    return ops_root(processed_dir, source) / "retire_pending.json"


def read_retire_pending(processed_dir: Path, source: str) -> list[str]:
    """The pending part names; ``[]`` when the file does not exist. A file
    that is not a JSON list of strings raises ``ValueError`` (never guess
    which rows to delete)."""
    p = retire_pending_path(processed_dir, source)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    data = json.loads(text)
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ValueError(f"{p}: not a JSON list of part names")
    return data


def write_retire_pending(processed_dir: Path, source: str, names: list[str]) -> None:
    """Replace the pending file (atomic: temp file, fsync, rename)."""
    write_marker(retire_pending_path(processed_dir, source), sorted(set(names)))  # type: ignore[arg-type]


def add_retire_pending(processed_dir: Path, source: str, names: list[str]) -> None:
    """Add ``names`` to the pending file. Serialized across this process's
    worker threads (a read-modify-write); processes are serialized by the
    heavy lock."""
    with _PENDING_LOCK:
        write_retire_pending(
            processed_dir, source, [*read_retire_pending(processed_dir, source), *names]
        )


def _is_loaded(processed_dir: Path, source: str, name: str) -> bool:
    """True when the shard of part ``name`` has a load_success marker (the
    loader names it by shard file name: ``.parquet`` or ``.jsonl``)."""
    return any(
        checkpoint_markers.load_success_marker_path(processed_dir, source, f"{name}{ext}").is_file()
        for ext in _SHARD_EXTS
    )


def classify_pending(
    processed_dir: Path, source: str, pending: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """``(to_delete, done, waiting)`` for the pending part names. The load
    marker is checked FIRST:

    - a load_success marker (``.parquet.ok`` or ``.jsonl.ok``): a load
      replaced the rows of this name by source_file after the name was
      recorded, because serialize records the name before it unlinks the load
      marker of the part. Drop the name, with or without a success marker:
      ``load_articles`` loads every staged shard, also one that a stopped
      serialize left without a success marker. (A crash between the record and
      the unlink leaves the OLD load marker; the name is dropped here, and the
      next serialize that deletes the part sees the marker and records the
      name again.)
    - a success marker only: the part was written again but not loaded yet
      (for example ``--skip-unit``). Its old rows may hold CIDs that the new
      part does not, so keep the name pending and delete nothing.
    - neither: the part is gone; delete its rows."""
    to_delete: list[str] = []
    done: list[str] = []
    waiting: list[str] = []
    for name in pending:
        if _is_loaded(processed_dir, source, name):
            done.append(name)
        elif checkpoint_markers.success_marker_path(processed_dir, source, name).is_file():
            waiting.append(name)
        else:
            to_delete.append(name)
    return to_delete, done, waiting


def _retire(
    processed_dir: Path,
    run_id: str | None,
    *,
    source: str,
    read_pending: Callable[[Path], list[str]] | None = None,
    write_pending: Callable[[Path, list[str]], None] | None = None,
) -> int:
    """``read_pending`` / ``write_pending`` default to this module's helpers
    for ``source``; an entry point may pass its own (pubchem passes its
    ``serialize_pubchem`` wrappers)."""
    if read_pending is None:

        def read_pending(d: Path) -> list[str]:
            return read_retire_pending(d, source)

    if write_pending is None:

        def write_pending(d: Path, names: list[str]) -> None:
            write_retire_pending(d, source, names)

    # A first read only decides whether a lock is needed at all.
    try:
        pending = read_pending(processed_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not pending:
        print(f"{source}: no deleted part pending; nothing retired")
        return 0

    from episteme.data.db.connection import connection
    from episteme.ops import lock
    from episteme.ops import status as ops_status
    from episteme.ops.stop import stage_guard

    holder: list = [None]  # the connection of the running delete, for stage_guard
    # The heavy lock covers the pending file: serialize writes it under the same
    # lock, so the file is read again, classified and written back inside it.
    with stage_guard(lambda: holder[0], lock_stage="load", source=source) as g:
        try:
            pending = read_pending(processed_dir)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return g.exit_code(1)
        to_delete, done, waiting = classify_pending(processed_dir, source, pending)
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
                            conn, source=source, source_files=to_delete, run_id=run_id
                        )
                        conn.commit()
                    finally:
                        holder[0] = None
        removed = set(to_delete) | set(done)
        if removed:
            # After the commit: a stop before this write leaves the names
            # pending, and the next run deletes them again (0 rows). Only the
            # names this run deleted or dropped leave the file.
            current_pending = read_pending(processed_dir)
            write_pending(processed_dir, [n for n in current_pending if n not in removed])
        print(
            f"{source}: retired {deleted} article(s) of {len(to_delete)} deleted part(s); "
            f"{len(waiting)} part(s) stay pending"
        )
        return g.exit_code(0)


def main(
    argv: list[str] | None = None,
    *,
    source: str,
    retire: Callable[[Path, str | None], int] | None = None,
) -> int:
    """``retire`` overrides the core (an entry point that must keep its own
    hooks); the default is ``_retire`` for ``source``."""
    settings = get_settings()
    p = argparse.ArgumentParser(
        prog=f"python -m episteme.data.{source}.retire",
        description=f"Retire the rows of deleted {source} parts from articles/article_body.",
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
    if retire is not None:
        return retire(args.processed_dir, settings.run_id)
    return _retire(args.processed_dir, settings.run_id, source=source)
