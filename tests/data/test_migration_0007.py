import psycopg
import pytest

from ._graph_schema import MIG, REPO

pytestmark = pytest.mark.pg

MIGRATION_0007 = MIG / "0007_pubmed_partition.sql"


def _base_schema(conn) -> None:
    """schema.sql + 0002-0006, without 0007 (the precondition test needs a pre-0007 state)."""
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute((REPO / "src/episteme/data/db/extensions.sql").read_text(encoding="utf-8"))
        cur.execute((REPO / "src/episteme/data/db/schema.sql").read_text(encoding="utf-8"))
        for name in sorted(p.name for p in MIG.glob("000[2-6]_*.sql")):
            cur.execute((MIG / name).read_text(encoding="utf-8"))
    conn.commit()


def _insert_pubmed(cur, pmid: str, year: int | None) -> None:
    cur.execute(
        "INSERT INTO episteme.articles (id, source, source_record_id, pmid, year, license, "
        "source_file) VALUES (%s, 'pubmed', %s, %s, %s, 'open_metadata', 'f.xml.gz')",
        (f"pubmed:{pmid}", pmid, pmid, year),
    )
    cur.execute(
        "INSERT INTO episteme.article_body (article_id, source, year, title) "
        "VALUES (%s, 'pubmed', %s, 't')",
        (f"pubmed:{pmid}", year),
    )


def test_0007_routes_pubmed_rows_to_their_own_partitions(pg_conn):
    _base_schema(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(MIGRATION_0007.read_text(encoding="utf-8"))
        cur.execute(MIGRATION_0007.read_text(encoding="utf-8"))  # idempotent
        cur.execute(
            "SELECT count(*) FROM pg_inherits "
            "WHERE inhparent = 'episteme.articles_pubmed'::regclass"
        )
        assert cur.fetchone()[0] == 14  # 13 year ranges + DEFAULT
        _insert_pubmed(cur, "1", 2021)
        _insert_pubmed(cur, "2", 0)
        _insert_pubmed(cur, "3", 1781)
        _insert_pubmed(cur, "4", None)  # e.g. enrich_from_lite setting a NULL year
        cur.execute(
            "SELECT tableoid::regclass::text, pmid FROM episteme.articles "
            "WHERE source = 'pubmed' ORDER BY pmid"
        )
        assert cur.fetchall() == [
            ("episteme.articles_pubmed_2020_2025", "1"),
            ("episteme.articles_pubmed_y0", "2"),
            ("episteme.articles_pubmed_pre1950", "3"),
            ("episteme.articles_pubmed_default", "4"),
        ]
        cur.execute("SELECT DISTINCT tableoid::regclass::text FROM episteme.article_body")
        assert cur.fetchall() == [("episteme.article_body_pubmed",)]
        # the load-path indexes (migration 0004) cascade to the new partitions
        cur.execute(
            "SELECT count(*) FROM pg_indexes WHERE schemaname = 'episteme' "
            "AND tablename = 'articles_pubmed_2020_2025' AND indexdef LIKE %s",
            ("%(source, source_file)%",),
        )
        assert cur.fetchone()[0] == 1
    pg_conn.rollback()


def test_0007_refuses_while_default_holds_pubmed_rows(pg_conn):
    _base_schema(pg_conn)
    with pg_conn.cursor() as cur:
        _insert_pubmed(cur, "1", 2021)  # lands in the DEFAULT partitions
    pg_conn.commit()
    with pg_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(MIGRATION_0007.read_text(encoding="utf-8"))
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('episteme.articles_pubmed')")
        assert cur.fetchone()[0] is None  # one transaction: nothing half-applied
