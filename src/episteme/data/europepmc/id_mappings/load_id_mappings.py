"""Load Europe PMC's ``PMID_PMCID_DOI.csv.gz`` id-mappings feed into
``episteme.id_map`` — a small cross-reference table (PMID / PMCID / DOI
triples), NOT an ``episteme.articles`` source. No ``article_schema`` /
``jats.py`` involvement, no staging shard, no field-shape ``--report``.

Real file (confirmed 2026-09-11 via an HTTP range read of
``https://ftp.ebi.ac.uk/pub/databases/pmc/DOI/PMID_PMCID_DOI.csv.gz``, ~342MB):
  * a header row ``PMID,PMCID,DOI`` (skipped if present; a file lacking it is
    still handled -- see ``_looks_like_header``).
  * plain comma-delimited CSV, standard ``"``-quoting (an absent value is a
    quoted empty string ``""``; the DOI column holds a full
    ``https://doi.org/10....`` URL, not a bare DOI -- stored as-is, unchanged).
  * PMID is frequently empty (PMCID/DOI-only rows are common).

Load strategy: stream the CSV (``csv.reader`` over ``gzip.open(path, "rt")``,
never loading the 342MB file into memory) into a session-temp staging table via
``COPY ... FROM STDIN``, then one set-based
``INSERT ... SELECT ... ON CONFLICT (coalesce(pmid,''), coalesce(pmcid,''),
coalesce(doi,'')) DO NOTHING`` against ``episteme.id_map``'s existing
expression-based unique index ``id_map_ids_uq`` (see ``db/schema.sql``) --
confirmed empirically (against ``episteme_test``) that Postgres CAN target an
expression index this way when the ``ON CONFLICT`` expressions are repeated
verbatim; no ``WHERE NOT EXISTS`` fallback was needed.

``force`` is accepted for CLI-flag symmetry with the other SP2 loaders but is a
no-op: the load is already dedupe-safe via ``ON CONFLICT DO NOTHING``, so a
re-run of an unchanged file naturally inserts 0 new rows -- that IS the
idempotency the brief asks for, not a delete-then-reload.

This module never reads ``os.environ`` -- ``load_id_mappings`` takes a ``conn``
directly (the caller, e.g. ``main()``, owns DSN resolution via
``episteme.data.db.connection.connection()`` / ``episteme.config``).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SOURCE = "europepmc_id_mappings"

_EXPECTED_HEADER = ("pmid", "pmcid", "doi")


def _is_valid_gzip(path: Path) -> bool:
    """Stream-decompress the whole file, discarding output, to confirm the
    gzip member is complete and uncorrupted -- cheaper than re-reading it a
    second time for the real CSV pass, and catches truncation up front."""
    try:
        with gzip.open(path, "rb") as f:
            while f.read(1 << 20):
                pass
        return True
    except (OSError, EOFError, zlib.error):
        return False


def _norm(value: str | None) -> str | None:
    """CSV empty string -> NULL, so ``coalesce(col, '')`` treats a genuinely
    missing field the same whether the source CSV wrote nothing or ``""``."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def _looks_like_header(row: list[str]) -> bool:
    return [c.strip().lower() for c in row[:3]] == list(_EXPECTED_HEADER)


def load_id_mappings(gz_path: Path, conn, *, force: bool = False) -> dict[str, Any]:
    """Load one ``PMID_PMCID_DOI.csv.gz`` file into ``episteme.id_map``.

    Runs in the caller's transaction (does not commit). On a corrupt/truncated
    gzip file, returns ``{"error": "..."}`` WITHOUT touching the DB at all --
    checked before the temp table is even created.

    Returns (success case):
        {"rows_read": int, "rows_inserted": int, "rows_skipped_dupe": int}

    ``force`` is accepted for CLI symmetry with the other SP2 loaders; it is a
    no-op here -- see module docstring.
    """
    gz_path = Path(gz_path)
    _ = force  # no-op: ON CONFLICT DO NOTHING is already idempotent (see module docstring)

    if not _is_valid_gzip(gz_path):
        return {"error": f"corrupt or truncated gzip file: {gz_path}"}

    source_file = gz_path.name
    retrieved_at = datetime.now(timezone.utc)
    rows_read = 0

    with conn.cursor() as cur:
        # DROP first: a caller re-invoking on the same conn/transaction without
        # an intervening commit (ON COMMIT DROP fires at commit, not at the end
        # of this function) would otherwise hit "relation already exists".
        cur.execute("DROP TABLE IF EXISTS _id_map_stage")
        cur.execute(
            "CREATE TEMP TABLE _id_map_stage (pmid text, pmcid text, doi text) ON COMMIT DROP"
        )

        with (
            gzip.open(gz_path, "rt", newline="", encoding="utf-8") as fh,
            cur.copy("COPY _id_map_stage (pmid, pmcid, doi) FROM STDIN") as cp,
        ):
            reader = csv.reader(fh)
            first = next(reader, None)
            if first is not None and not _looks_like_header(first):
                pmid, pmcid, doi = (first + ["", "", ""])[:3]
                cp.write_row((_norm(pmid), _norm(pmcid), _norm(doi)))
                rows_read += 1
            for row in reader:
                if not row:
                    continue
                pmid, pmcid, doi = (row + ["", "", ""])[:3]
                cp.write_row((_norm(pmid), _norm(pmcid), _norm(doi)))
                rows_read += 1

        # Confirmed empirically against episteme_test: ON CONFLICT CAN target
        # id_map_ids_uq (an expression-based unique index) by repeating its
        # exact coalesce(...) expressions here.
        cur.execute(
            """
            INSERT INTO episteme.id_map (pmid, pmcid, doi, source_file, retrieved_at)
            SELECT pmid, pmcid, doi, %(source_file)s, %(retrieved_at)s
              FROM _id_map_stage
            ON CONFLICT (coalesce(pmid, ''), coalesce(pmcid, ''), coalesce(doi, '')) DO NOTHING
            """,
            {"source_file": source_file, "retrieved_at": retrieved_at},
        )
        rows_inserted = cur.rowcount

    return {
        "rows_read": rows_read,
        "rows_inserted": rows_inserted,
        "rows_skipped_dupe": rows_read - rows_inserted,
    }


def main(argv: list[str] | None = None) -> int:
    from episteme.config import get_settings
    from episteme.data.db.connection import connection

    settings = get_settings()

    p = argparse.ArgumentParser(
        prog="python -m episteme.data.europepmc.id_mappings.load_id_mappings",
        description="Load Europe PMC PMID_PMCID_DOI.csv.gz into episteme.id_map.",
    )
    p.add_argument(
        "--raw-dir",
        type=Path,
        default=settings.raw_root / "europepmc" / "id_mappings",
        help="directory holding the downloaded *.csv.gz (default: 01_raw/europepmc/id_mappings)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="accepted for CLI symmetry; no-op (ON CONFLICT DO NOTHING is already idempotent)",
    )
    p.add_argument(
        "--reason",
        type=str,
        default=None,
        help="accepted for CLI symmetry with run_pipeline.sh's --force --reason; unused",
    )
    args = p.parse_args(argv)

    raw_dir: Path = args.raw_dir
    if not raw_dir.is_dir():
        print(f"ERROR: raw dir not found: {raw_dir}", file=sys.stderr)
        return 1

    candidates = sorted(raw_dir.glob("*.csv.gz"))
    if not candidates:
        print(f"ERROR: no *.csv.gz under {raw_dir}", file=sys.stderr)
        return 1
    if len(candidates) > 1:
        print(
            f"ERROR: multiple *.csv.gz under {raw_dir}, expected exactly one: "
            f"{[c.name for c in candidates]}",
            file=sys.stderr,
        )
        return 1
    gz_path = candidates[0]

    print(f"source={SOURCE} gz_path={gz_path}")
    with connection() as conn:
        result = load_id_mappings(gz_path, conn, force=args.force)
        if "error" in result:
            conn.rollback()
            print(f"ERROR: {result['error']}", file=sys.stderr)
            return 1
        conn.commit()

    print(
        f"done rows_read={result['rows_read']} rows_inserted={result['rows_inserted']} "
        f"rows_skipped_dupe={result['rows_skipped_dupe']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
