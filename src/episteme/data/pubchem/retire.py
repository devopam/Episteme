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
   (``classify_pending``, the load marker first): a load_success marker (a
   load replaced the rows of the name after it was recorded, with or without
   a part success marker) -> drop the name; a success marker only (written
   again, not loaded yet, e.g. ``--skip-unit``) -> keep the name pending and
   delete nothing; neither -> delete its rows.
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

The logic lives in ``episteme.data.parts_retire`` (shared with pmc); this module
is the pubchem entry point and keeps the names its tests use.
"""

from __future__ import annotations

from pathlib import Path

from episteme.data import parts_retire, postgres_loader  # noqa: F401 (tests patch postgres_loader)
from episteme.data.pubchem.serialize_pubchem import (
    SOURCE,
    read_retire_pending,
    write_retire_pending,
)


def classify_pending(
    processed_dir: Path, pending: list[str]
) -> tuple[list[str], list[str], list[str]]:
    return parts_retire.classify_pending(processed_dir, SOURCE, pending)


def _retire(processed_dir: Path, run_id: str | None) -> int:
    # The pending-file helpers are looked up here, at call time, so a patch of
    # this module's names takes effect.
    return parts_retire._retire(
        processed_dir,
        run_id,
        source=SOURCE,
        read_pending=lambda d: read_retire_pending(d),
        write_pending=lambda d, names: write_retire_pending(d, names),
    )


def main(argv: list[str] | None = None) -> int:
    return parts_retire.main(argv, source=SOURCE, retire=_retire)


if __name__ == "__main__":
    raise SystemExit(main())
