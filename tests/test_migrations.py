import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.pg  # applies 0002 to episteme_test

REPO = Path(__file__).resolve().parents[1]
MIGRATION_0002 = REPO / "src/episteme/data/db/migrations/0002_container_and_book_parts.sql"


@pytest.fixture(scope="module")
def sa_conn():
    import psycopg

    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN not set")
    with psycopg.connect(dsn, autocommit=True) as c:
        # 0002 is idempotent; apply it here so these assertions hold regardless of
        # test-ordering (other pg tests DROP SCHEMA episteme CASCADE + reload
        # schema.sql only) and of migrate_database.sh dying at 0001 on builds
        # without pgvector.
        with c.cursor() as cur:
            cur.execute(MIGRATION_0002.read_text(encoding="utf-8"))
        yield c


def test_migration_0002_adds_container_columns(sa_conn):
    with sa_conn.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='episteme' AND table_name='articles'
              AND column_name IN ('container_id','book_meta')
        """)
        cols = {r[0] for r in cur.fetchall()}
    assert cols == {"container_id", "book_meta"}


def test_migration_0002_article_parts_table_and_partitions(sa_conn):
    with sa_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('episteme.article_parts')")
        assert cur.fetchone()[0] is not None
        cur.execute("""
            SELECT count(*) FROM pg_inherits
            JOIN pg_class p ON p.oid = inhparent
            WHERE p.relname = 'article_parts'
        """)
        assert cur.fetchone()[0] == 8  # 8 hash buckets


def test_migration_0002_bookshelf_partition(sa_conn):
    with sa_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('episteme.articles_bookshelf')")
        assert cur.fetchone()[0] is not None
        cur.execute("SELECT to_regclass('episteme.articles_bookshelf_y0')")
        assert cur.fetchone()[0] is not None
