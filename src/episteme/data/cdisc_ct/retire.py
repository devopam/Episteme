"""CLI: retire superseded CDISC CT releases from episteme.articles /
episteme.article_body once the CURRENT release per package has loaded.

    python -m episteme.data.cdisc_ct.retire [--raw-dir ...] [--processed-dir ...]

Retires PER PACKAGE. For each package ``discover_cdisc_ct_files`` finds
locally (its newest release), the kept ``source_file`` is that release's
``input_key(path, raw_dir)`` -- exactly what ``serialize_cdisc_ct.py`` writes
into ``episteme.articles.source_file`` -- and the delete is scoped to that
package's own rows via ``scope_prefix=f"{package}__"`` (``input_key`` is
``<Package>__<date>__<Package>_Terminology.txt``). So:

- a package present locally and loaded: its older releases are retired;
- a package present locally but whose newest shard has no ``load_success``
  marker yet: skipped (its old rows stay until its new rows are in);
- a package NOT present in the local raw tree (for example after a
  ``--max-files`` partial download on another machine): never touched.

An earlier version built one keep-set across all packages and deleted every
``cdisc_ct`` row outside it, which silently deleted every package missing
from the local tree -- fixed in SP7 Task 4 review round 1.

``load_success`` markers key on the SHARD FILE name, not the bare
``input_key``: ``staging_writer`` appends ``.parquet`` (or ``.jsonl`` when
pyarrow is unavailable), so the marker check tries both extensions.
If no CDISC CT release files are discovered at all, this is a no-op (exit 0);
``retire_source_files`` is never called with an empty keep-set.

``--force``/``--reason`` are accepted and ignored: ``load_cdisc_ct.sh``
forwards its own ``"$@"`` (run_pipeline.sh's ``load_args``, which is
``--force --reason R`` on a forced run, else empty) to this script too, so
those flags must parse rather than fail this stage after a successful load.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from episteme.config import get_settings
from episteme.data import checkpoint_markers, postgres_loader
from episteme.data.cdisc_ct.serialize_cdisc_ct import SOURCE, discover_cdisc_ct_files
from episteme.data.db.connection import connection

_SHARD_EXTS: tuple[str, ...] = (".parquet", ".jsonl")


def _is_loaded(processed_dir: Path, source: str, basename: str) -> bool:
    """True if ``basename``'s staging shard has a load_success marker, under
    either extension staging_writer may have produced (see module docstring)."""
    return any(
        checkpoint_markers.load_success_marker_path(
            processed_dir, source, f"{basename}{ext}"
        ).is_file()
        for ext in _SHARD_EXTS
    )


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()

    p = argparse.ArgumentParser(
        prog="python -m episteme.data.cdisc_ct.retire",
        description="Retire superseded CDISC CT releases from episteme.articles/article_body.",
    )
    p.add_argument("--raw-dir", type=Path, default=settings.raw_root / "cdisc_ct")
    p.add_argument("--processed-dir", type=Path, default=settings.processed_root)
    p.add_argument("--force", action="store_true", help="accepted, unused (see module docstring)")
    p.add_argument(
        "--reason", type=str, default=None, help="accepted, unused (see module docstring)"
    )
    args = p.parse_args(argv)

    raw_dir: Path = args.raw_dir
    processed_dir: Path = args.processed_dir

    files = discover_cdisc_ct_files(raw_dir)
    if not files:
        print(f"no CDISC CT release files under {raw_dir}; nothing to retire")
        return 0

    plan: list[tuple[str, str]] = []  # (package prefix, kept source_file)
    for f in files:
        package = f.parent.parent.name
        basename = checkpoint_markers.input_key(f, raw_dir)
        if not _is_loaded(processed_dir, SOURCE, basename):
            print(f"{package}: {basename} not loaded yet (no load_success marker) -- skipped")
            continue
        plan.append((f"{package}__", basename))

    if not plan:
        print("no loaded CDISC CT release to retire against; nothing retired")
        return 0

    run_id = settings.run_id
    total = 0
    with connection() as conn:
        for prefix, keep in plan:
            deleted = postgres_loader.retire_source_files(
                conn,
                source=SOURCE,
                keep_source_files=[keep],
                run_id=run_id,
                scope_prefix=prefix,
            )
            print(f"{prefix[:-2]}: retired {deleted} article(s); kept {keep}")
            total += deleted
        conn.commit()

    print(f"retired {total} article(s) across {len(plan)} package(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
