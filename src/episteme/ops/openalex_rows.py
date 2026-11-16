"""Do any openalex rows exist? Used by run_pipeline.sh before a pubmed/pmc ``--bulk`` load.

``--bulk`` drops the key indexes that the openalex sweep of a pubmed/pmc load needs
(Ruling R2), so run_pipeline.sh refuses it while ``episteme.articles_openalex`` holds a
row. Exit 0: migration 0012 present and no rows. Exit 1: rows exist. Exit 2: error, also when
migration 0012 is missing (the loader would refuse too).
"""

from __future__ import annotations

import sys

_NEEDS = (
    ("episteme.articles_openalex", "SELECT to_regclass('episteme.articles_openalex') IS NOT NULL"),
    (
        "episteme.article_body_openalex",
        "SELECT to_regclass('episteme.article_body_openalex') IS NOT NULL",
    ),
    (
        "episteme._lineage.dedup_counts",
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = "
        "'episteme' AND table_name = '_lineage' AND column_name = 'dedup_counts')",
    ),
)


class MigrationMissing(RuntimeError):
    pass


def openalex_rows_exist(conn) -> bool:
    """True when openalex rows exist. Raises MigrationMissing without migration 0012."""
    with conn.cursor() as cur:
        missing = []
        for name, sql in _NEEDS:
            cur.execute(sql)
            if not cur.fetchone()[0]:
                missing.append(name)
        if missing:
            raise MigrationMissing("missing " + ", ".join(missing) + ": apply migration 0012")
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
