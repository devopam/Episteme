"""Do any openalex rows exist? Used by run_pipeline.sh before a pubmed/pmc ``--bulk`` load.

``--bulk`` drops the key indexes that the openalex sweep of a pubmed/pmc load needs
(Ruling R2), so run_pipeline.sh refuses it while ``episteme.articles_openalex`` holds a
row. Exit 0: no rows (or no table). Exit 1: rows exist. Exit 2: error.
"""

from __future__ import annotations

import sys


def openalex_rows_exist(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('episteme.articles_openalex') IS NOT NULL")
        if not cur.fetchone()[0]:
            return False
        cur.execute("SELECT EXISTS (SELECT 1 FROM episteme.articles_openalex)")
        return bool(cur.fetchone()[0])


def main(argv: list[str] | None = None) -> int:
    from episteme.data.db.connection import connection

    try:
        with connection() as conn:
            return 1 if openalex_rows_exist(conn) else 0
    except Exception as exc:  # noqa: BLE001 - any failure is exit 2
        print(f"openalex_rows: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
