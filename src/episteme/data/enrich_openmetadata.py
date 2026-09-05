"""CLI: write + validate the OpenMetadata catalog manifest for one source.

Steps:
  1. reject an unknown ``--source`` (exit 2);
  2. build the manifest (``openmetadata_manifest.build_manifest`` -- this
     already validates it against the bundled schema);
  3. write it to
     ``<processed-dir>/_ops/<source>/catalog/<source>.openmetadata.json``
     (``json.dumps(..., indent=2, sort_keys=True)``);
  4. re-validate the file that was just written;
  5. best-effort audit: record a ``config_change`` event -- any failure
     (DB down, actor unset, mirror error) prints a ``warning:`` line and is
     swallowed so the file write never depends on the DB;
  6. print the path, exit 0.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import jsonschema

from episteme.config import get_settings
from episteme.data import article_schema
from episteme.data.openmetadata_manifest import _SCHEMA, build_manifest

_DEFAULT_TABLES = "articles,article_body,article_cites,article_mesh,id_map"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="enrich_openmetadata",
        description="Build and validate the OpenMetadata catalog manifest for a source.",
    )
    p.add_argument("--source", required=True, help="one of episteme.data.article_schema.SOURCES")
    p.add_argument(
        "--tables",
        default=_DEFAULT_TABLES,
        help=f"comma-separated table names (default: {_DEFAULT_TABLES})",
    )
    p.add_argument(
        "--processed-dir",
        default=None,
        help="processed-data root (default: config.get_settings().processed_root)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.source not in article_schema.SOURCES:
        print(
            f"error: unknown source {args.source!r}; "
            f"must be one of {sorted(article_schema.SOURCES)}",
            file=sys.stderr,
        )
        return 2

    processed_dir = (
        Path(args.processed_dir) if args.processed_dir else get_settings().processed_root
    )
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]

    manifest = build_manifest(args.source, tables=tables)

    out_path = processed_dir / "_ops" / args.source / "catalog" / f"{args.source}.openmetadata.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    # Re-validate what actually landed on disk.
    jsonschema.validate(json.loads(out_path.read_text(encoding="utf-8")), _SCHEMA)

    _best_effort_audit(args.source)

    print(str(out_path))
    return 0


def _best_effort_audit(source: str) -> None:
    """Record a ``config_change`` audit row; never fatal."""
    try:
        from episteme import audit_trail
        from episteme.data.db.connection import connection

        with connection() as conn:
            audit_trail.record(
                "config_change",
                conn=conn,
                object=f"om-manifest {source}",
                run_id=get_settings().run_id or f"om-{source}",
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - the file write must not depend on the DB
        print(f"warning: audit record skipped ({exc!r})", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
