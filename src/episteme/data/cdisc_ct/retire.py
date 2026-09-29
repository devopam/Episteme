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

I1/I2 (whole-branch review, Ruling FW7-1):

- ``--print-current-shards`` prints one staging shard file name per package
  (``current_shard_names``) -- the exact ``--only`` set ``load_cdisc_ct.sh``
  now passes to ``load_articles`` so an older release's shard sitting in the
  same staging directory can never load after the newest one.
- Before retiring a package, ``main()`` also verifies the DATABASE (not just
  the local raw tree + marker) agrees the kept release is current: the row
  count for the kept ``source_file`` must match its ``load_success``
  marker's recorded count (or be > 0 if the marker has no usable count), and
  no row for that package may carry a date segment newer than the kept
  release's. Either check failing skips that package (prints a warning
  naming it) without deleting anything -- see ``_verify_before_retire``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from episteme.config import get_settings
from episteme.data import checkpoint_markers, postgres_loader
from episteme.data.cdisc_ct.serialize_cdisc_ct import (
    SOURCE,
    discover_cdisc_ct_files,
    release_date_of,
)
from episteme.data.db.connection import connection

_SHARD_EXTS: tuple[str, ...] = (".parquet", ".jsonl")


def _load_success_marker_file(processed_dir: Path, source: str, basename: str) -> Path | None:
    """The load_success marker file for ``basename``'s staging shard, under
    whichever extension staging_writer actually produced (see module
    docstring), or ``None`` if neither exists."""
    for ext in _SHARD_EXTS:
        p = checkpoint_markers.load_success_marker_path(processed_dir, source, f"{basename}{ext}")
        if p.is_file():
            return p
    return None


def _is_loaded(processed_dir: Path, source: str, basename: str) -> bool:
    """True if ``basename``'s staging shard has a load_success marker, under
    either extension staging_writer may have produced (see module docstring)."""
    return _load_success_marker_file(processed_dir, source, basename) is not None


def _marker_row_count(marker_path: Path) -> int | None:
    """The inserted-row count recorded in a ``load_success`` marker's JSON
    payload (``stats.rows``, written by ``load_articles._write_load_success``
    with ``load_source_file``'s own result dict). ``None`` if the marker is
    unreadable or carries no usable integer count."""
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    stats = payload.get("stats")
    if not isinstance(stats, dict):
        return None
    rows = stats.get("rows")
    return rows if isinstance(rows, int) else None


def _package_date(source_file: str) -> str | None:
    """The date segment of a ``<Package>__<date>__<Package>_Terminology.txt``
    ``source_file`` (``input_key``'s own ``__``-joined shape), or ``None`` if
    it doesn't have one -- ISO ``YYYY-MM-DD`` sorts correctly as a string, so
    no date parsing is needed to compare two of these."""
    parts = source_file.split("__")
    return parts[1] if len(parts) >= 2 else None


def _verify_before_retire(
    conn,
    *,
    source: str,
    package: str,
    keep: str,
    keep_date: str,
    expected_rows: int | None,
) -> tuple[bool, str]:
    """I2: confirm the DATABASE agrees the kept release is current before
    retire_source_files deletes anything for this package. Returns
    ``(True, "")`` only when BOTH hold:

    (a) ``select count(*) from episteme.articles where source=<source> and
        source_file=<keep>`` equals ``expected_rows`` (the kept release's own
        ``load_success`` marker row count); if ``expected_rows`` has no
        usable value, this instead requires the count to be > 0.
    (b) no ``episteme.articles`` row for this package (``source_file``
        starting with ``f"{package}__"``) carries a date segment newer than
        ``keep_date``.

    Never deletes anything itself; the caller skips (prints a warning naming
    the package) on a ``False`` return."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM episteme.articles WHERE source = %s AND source_file = %s",
            (source, keep),
        )
        (actual,) = cur.fetchone()

    if expected_rows is not None and expected_rows > 0:
        if actual != expected_rows:
            return False, (
                f"db row count for {keep!r} is {actual}, its load_success marker "
                f"recorded {expected_rows} -- refusing to retire without a confirmed match"
            )
    elif actual == 0:
        return False, (
            f"db holds 0 rows for the kept release {keep!r} (its load_success marker "
            "has no usable row count either) -- refusing to retire"
        )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT source_file FROM episteme.articles "
            "WHERE source = %s AND starts_with(source_file, %s)",
            (source, f"{package}__"),
        )
        seen = [r[0] for r in cur.fetchall()]

    for sf in seen:
        d = _package_date(sf)
        if d is not None and d > keep_date:
            return False, (
                f"db holds a newer release {sf!r} than the local newest "
                f"{keep!r} -- refusing to retire"
            )

    return True, ""


def current_shard_names(raw_dir: Path, processed_dir: Path) -> list[str]:
    """One staging shard file name per package (I1): for each package
    ``discover_cdisc_ct_files`` finds locally under ``raw_dir`` (its newest
    release) that has ALSO been serialized (its staging shard file exists
    under ``processed_dir/staging/cdisc_ct``, under whichever extension
    staging_writer actually produced), the shard's file name. A package
    whose newest release has not been serialized into a shard yet is simply
    absent from the result -- nothing to load for it yet.

    This is the exact ``--only`` set ``load_cdisc_ct.sh`` passes to
    ``load_articles``, so an older release's shard sitting in the same
    staging directory (left over from before this run's newest release was
    discovered/serialized) can never load after -- or instead of -- the
    newest one."""
    staging_dir = Path(processed_dir) / "staging" / SOURCE
    names: list[str] = []
    for f in discover_cdisc_ct_files(raw_dir):
        basename = checkpoint_markers.input_key(f, raw_dir)
        for ext in _SHARD_EXTS:
            candidate = staging_dir / f"{basename}{ext}"
            if candidate.is_file():
                names.append(candidate.name)
                break
    return names


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
    p.add_argument(
        "--print-current-shards",
        action="store_true",
        help=(
            "print each package's newest, already-serialized staging shard file name "
            "(one per line) and exit -- the --only set for load_cdisc_ct.sh (I1)"
        ),
    )
    args = p.parse_args(argv)

    raw_dir: Path = args.raw_dir
    processed_dir: Path = args.processed_dir

    if args.print_current_shards:
        for name in current_shard_names(raw_dir, processed_dir):
            print(name)
        return 0

    files = discover_cdisc_ct_files(raw_dir)
    if not files:
        print(f"no CDISC CT release files under {raw_dir}; nothing to retire")
        return 0

    plan: list[dict[str, str | int | None]] = []
    for f in files:
        package = f.parent.parent.name
        basename = checkpoint_markers.input_key(f, raw_dir)
        marker = _load_success_marker_file(processed_dir, SOURCE, basename)
        if marker is None:
            print(f"{package}: {basename} not loaded yet (no load_success marker) -- skipped")
            continue
        plan.append(
            {
                "package": package,
                "prefix": f"{package}__",
                "keep": basename,
                "keep_date": release_date_of(f),
                "expected_rows": _marker_row_count(marker),
            }
        )

    if not plan:
        print("no loaded CDISC CT release to retire against; nothing retired")
        return 0

    run_id = settings.run_id
    total = 0
    with connection() as conn:
        for entry in plan:
            ok, reason = _verify_before_retire(
                conn,
                source=SOURCE,
                package=entry["package"],
                keep=entry["keep"],
                keep_date=entry["keep_date"],
                expected_rows=entry["expected_rows"],
            )
            if not ok:
                print(f"{entry['package']}: skipped -- {reason}")
                continue
            deleted = postgres_loader.retire_source_files(
                conn,
                source=SOURCE,
                keep_source_files=[entry["keep"]],
                run_id=run_id,
                scope_prefix=entry["prefix"],
            )
            print(f"{entry['package']}: retired {deleted} article(s); kept {entry['keep']}")
            total += deleted
        conn.commit()

    print(f"retired {total} article(s) across {len(plan)} package(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
