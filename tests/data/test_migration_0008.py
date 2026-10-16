import psycopg
import pytest

from ._graph_schema import MIG, REPO

pytestmark = pytest.mark.pg

MIGRATION_0008 = MIG / "0008_pmc_bookshelf_default_partition.sql"


def _base_schema(conn) -> None:
    """schema.sql + 0002-0007, without 0008 (to show the gap 0008 closes)."""
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute((REPO / "src/episteme/data/db/extensions.sql").read_text(encoding="utf-8"))
        cur.execute((REPO / "src/episteme/data/db/schema.sql").read_text(encoding="utf-8"))
        for name in sorted(p.name for p in MIG.glob("000[2-7]_*.sql")):
            cur.execute((MIG / name).read_text(encoding="utf-8"))
    conn.commit()


def _insert(cur, source: str, n: int, year: int | None) -> None:
    cur.execute(
        "INSERT INTO episteme.articles (id, source, source_record_id, year, license, "
        "source_file) VALUES (%s, %s, %s, %s, 'open_metadata', 'f.xml')",
        (f"{source}:{n}", source, str(n), year),
    )


def test_without_0008_a_null_pmc_year_fails_the_insert(pg_conn):
    _base_schema(pg_conn)
    with pg_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        _insert(cur, "pmc", 1, None)
    pg_conn.rollback()


def test_0008_routes_null_and_out_of_range_years_to_default(pg_conn):
    _base_schema(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(MIGRATION_0008.read_text(encoding="utf-8"))
        cur.execute(MIGRATION_0008.read_text(encoding="utf-8"))  # idempotent
        _insert(cur, "pmc", 1, 2021)
        _insert(cur, "pmc", 2, None)
        _insert(cur, "pmc", 3, 99999)
        _insert(cur, "bookshelf", 4, 0)
        _insert(cur, "bookshelf", 5, None)
        _insert(cur, "bookshelf", 6, -5)
        cur.execute("SELECT tableoid::regclass::text, id FROM episteme.articles ORDER BY id")
        assert cur.fetchall() == [
            ("episteme.articles_bookshelf_y0", "bookshelf:4"),
            ("episteme.articles_bookshelf_default", "bookshelf:5"),
            ("episteme.articles_bookshelf_default", "bookshelf:6"),
            ("episteme.articles_pmc_2020_2025", "pmc:1"),
            ("episteme.articles_pmc_default", "pmc:2"),
            ("episteme.articles_pmc_default", "pmc:3"),
        ]
        # parent indexes (e.g. migration 0004's load-path index) cascade to the new partitions
        cur.execute(
            "SELECT count(*) FROM pg_indexes WHERE schemaname = 'episteme' "
            "AND tablename IN ('articles_pmc_default', 'articles_bookshelf_default') "
            "AND indexdef LIKE %s",
            ("%(source, source_file)%",),
        )
        assert cur.fetchone()[0] == 2
    pg_conn.rollback()
