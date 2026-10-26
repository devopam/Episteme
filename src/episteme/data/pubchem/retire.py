"""CLI: retire the rows of deleted pubchem parts from episteme.articles /
episteme.article_body once every current part of a unit has loaded.

    python -m episteme.data.pubchem.retire [--processed-dir ...]

Why: ``serialize`` writes one unit (a ``CID-SMILES`` primary file) as parts
``<input_key>__partNNNNN``. A rebuild (``--force``, or a changed input) can
write fewer parts than before: it deletes the shards and markers of the
parts above the new count, but their rows stay in the database, because a
plain ``load`` replaces only the ``source_file`` values it loads.

What: for each unit whose primary success marker has the parts format
(``serialize_pubchem.read_primary_marker``: ``parts``, ``parts_list`` and
``inputs``), and whose current parts ALL have a ``load_success`` marker, one
``postgres_loader.retire_source_files`` call deletes the ``pubchem`` rows
whose ``source_file`` starts with ``f"{key}__part"`` and is not in
``parts_list``. The prefix scopes the delete to that unit; the keep-set is
the unit's current parts. ``retire_source_files`` records one audited
``load_replace`` event per call. A unit is skipped (a line is printed, no
row is deleted) when:

- it has no primary success marker (a serialize is not complete), or the
  marker has no parts format;
- its ``parts_list`` is empty (``retire_source_files`` refuses an empty
  keep-set);
- a current part has no ``load_success`` marker (not loaded yet, or its
  load failed).

``load_pubchem.sh`` runs this only after ``load_articles`` exited 0. A load
marker of a current part always belongs to the current content: serialize
deletes the load marker of every part it deletes or writes again.

``--force``, ``--reason`` and ``--skip-unit`` are accepted and ignored:
``load_pubchem.sh`` forwards run_pipeline.sh's ``load_args`` to this script
too. A skipped part has no load marker, so its unit is skipped here.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from episteme.config import get_settings
from episteme.data import checkpoint_markers, postgres_loader
from episteme.data.pubchem.serialize_pubchem import SOURCE, read_primary_marker

_SHARD_EXTS: tuple[str, ...] = (".parquet", ".jsonl")
_PART_NAME = re.compile(r".*__part\d{5,}$")


def _is_loaded(processed_dir: Path, part: str) -> bool:
    """True when the shard of ``part`` has a load_success marker. The loader
    names the marker by shard file name (``<part>.parquet`` or ``.jsonl``)."""
    return any(
        checkpoint_markers.load_success_marker_path(processed_dir, SOURCE, f"{part}{ext}").is_file()
        for ext in _SHARD_EXTS
    )


def retire_plan(processed_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """The units to retire and the skip lines. Touches no database.

    Each plan entry is ``{"key", "prefix", "keep"}``: the unit's input key, the
    scope prefix ``f"{key}__part"`` and the keep-set (its ``parts_list``)."""
    success_dir = checkpoint_markers.success_marker_path(processed_dir, SOURCE, "x").parent
    plan: list[dict[str, Any]] = []
    skipped: list[str] = []
    if not success_dir.is_dir():
        return plan, skipped
    for marker in sorted(success_dir.glob("*.ok")):
        key = marker.name[: -len(".ok")]
        if _PART_NAME.match(key):
            continue  # a part marker, not a unit
        st = read_primary_marker(processed_dir, key)
        if st is None:
            skipped.append(f"{key}: the primary success marker has no parts format -- skipped")
            continue
        keep = list(st["parts_list"])
        if not keep:
            skipped.append(f"{key}: the unit has 0 parts -- skipped (no keep-set)")
            continue
        missing = [p for p in keep if not _is_loaded(processed_dir, p)]
        if missing:
            skipped.append(
                f"{key}: {len(missing)} of {len(keep)} parts not loaded "
                f"(first: {missing[0]}) -- skipped"
            )
            continue
        plan.append({"key": key, "prefix": f"{key}__part", "keep": keep})
    return plan, skipped


def _retire(processed_dir: Path, run_id: str | None) -> int:
    from episteme.data.db.connection import connection
    from episteme.ops.stop import stage_guard

    plan, skipped = retire_plan(processed_dir)
    for line in skipped:
        print(line)
    if not plan:
        print("pubchem: no loaded unit to retire against; nothing retired")
        return 0

    # Only a run with work to do takes the locks (as cdisc_ct.retire does).
    with stage_guard(lambda: None, lock_stage="load", db_lock=True, source=SOURCE) as g:
        total = 0
        with connection() as conn:
            for entry in plan:
                deleted = postgres_loader.retire_source_files(
                    conn,
                    source=SOURCE,
                    keep_source_files=entry["keep"],
                    run_id=run_id,
                    scope_prefix=entry["prefix"],
                )
                print(
                    f"{entry['key']}: retired {deleted} article(s); kept {len(entry['keep'])} parts"
                )
                total += deleted
            conn.commit()
        print(f"pubchem: retired {total} article(s) across {len(plan)} unit(s)")
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
