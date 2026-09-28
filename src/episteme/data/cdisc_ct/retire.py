"""CLI: retire superseded CDISC CT releases from episteme.articles /
episteme.article_body once the CURRENT release per package has loaded.

    python -m episteme.data.cdisc_ct.retire [--raw-dir ...] [--processed-dir ...]

Keep-set = ``input_key(path, raw_dir)`` of each ``discover_cdisc_ct_files``
result (Task 2) -- exactly the value ``serialize_cdisc_ct.py`` writes into
``episteme.articles.source_file`` for that package's CURRENT release. A
package whose current release has not (yet) loaded is detected by its
``load_success`` marker: ``checkpoint_markers.load_success_marker_path``
keys on the SHARD FILE name, not the bare ``input_key`` -- ``staging_writer``
appends ``.parquet`` (or ``.jsonl`` when pyarrow is unavailable) to the
``input_key`` basename and strips no ``.txt`` suffix (confirmed against
``load_articles._write_load_success`` / ``_discover_shards``, and pinned by
``tests/data/test_serialize_cdisc_ct.py``) -- so the marker check here tries
both extensions.

If ANY discovered package lacks its load_success marker, this prints which
one and exits 0 WITHOUT deleting anything -- a partial retire (some packages'
old releases gone, others still pending their first load) is worse than a
no-op; run this again after the missing package's ``load`` stage completes.
If no CDISC CT release files are discovered at all, this is also a no-op
(exit 0) -- an empty raw tree must never reach ``retire_source_files`` with
an empty keep-set (that raises ``ValueError`` by design -- see
``postgres_loader.retire_source_files``).

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

    keep_source_files: list[str] = []
    for f in files:
        basename = checkpoint_markers.input_key(f, raw_dir)
        if not _is_loaded(processed_dir, SOURCE, basename):
            print(f"{basename}: not loaded yet (no load_success marker) -- skipping retire")
            return 0
        keep_source_files.append(basename)

    run_id = settings.run_id
    with connection() as conn:
        deleted = postgres_loader.retire_source_files(
            conn, source=SOURCE, keep_source_files=keep_source_files, run_id=run_id
        )
        conn.commit()

    print(f"retired {deleted} article(s); kept {len(keep_source_files)} current release(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
