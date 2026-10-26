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
2. A pending name that has a part success marker again is current: a later
   serialize wrote that part again (same part number). Its rows are current
   (or the next load replaces them), so it is never deleted; it is dropped
   from the file.
3. The other names: one ``postgres_loader.delete_source_files`` call deletes
   exactly their ``pubchem`` rows (``source_file = ANY(...)``, index-only),
   with one ``load_replace`` audit row, and commits. Then the names leave the
   file.

A stop between the commit and the file update leaves the names in the file;
the next run deletes them again (0 rows, one more audit row): idempotent.
A file that is not a JSON list of strings exits 1 before any database call.

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


def split_pending(processed_dir: Path, pending: list[str]) -> tuple[list[str], list[str]]:
    """``(to_delete, current)``: a pending name with a part success marker is
    current (written again by a later serialize) and is never deleted."""
    to_delete: list[str] = []
    current: list[str] = []
    for name in pending:
        if checkpoint_markers.success_marker_path(processed_dir, SOURCE, name).is_file():
            current.append(name)
        else:
            to_delete.append(name)
    return to_delete, current


def _retire(processed_dir: Path, run_id: str | None) -> int:
    try:
        pending = read_retire_pending(processed_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not pending:
        print("pubchem: no deleted part pending; nothing retired")
        return 0
    to_delete, current = split_pending(processed_dir, pending)
    for name in current:
        print(f"{name}: written again by a later serialize -- not retired")
    if not to_delete:
        write_retire_pending(processed_dir, [])
        print("pubchem: every pending part is current again; nothing retired")
        return 0

    from episteme.data.db.connection import connection
    from episteme.ops.stop import stage_guard

    with stage_guard(lambda: None, lock_stage="load", db_lock=True, source=SOURCE) as g:
        with connection() as conn:
            deleted = postgres_loader.delete_source_files(
                conn, source=SOURCE, source_files=to_delete, run_id=run_id
            )
            conn.commit()
        # After the commit: a stop before this line leaves the names pending,
        # and the next run deletes them again (0 rows).
        write_retire_pending(processed_dir, [])
        print(f"pubchem: retired {deleted} article(s) of {len(to_delete)} deleted part(s)")
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
