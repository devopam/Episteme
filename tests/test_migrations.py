import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.pg  # applies 0002 + 0003 to episteme_test

REPO = Path(__file__).resolve().parents[1]
MIGRATION_0002 = REPO / "src/episteme/data/db/migrations/0002_container_and_book_parts.sql"
MIGRATION_0003 = REPO / "src/episteme/data/db/migrations/0003_mesh_hierarchy.sql"


@pytest.fixture(scope="module")
def sa_conn():
    import psycopg

    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN not set")
    with psycopg.connect(dsn, autocommit=True) as c:
        # 0002 and 0003 are idempotent; apply them here so these assertions hold
        # regardless of test-ordering (other pg tests DROP SCHEMA episteme CASCADE +
        # reload schema.sql only) and of migrate_database.sh dying at 0001 on builds
        # without pgvector.
        with c.cursor() as cur:
            cur.execute(MIGRATION_0002.read_text(encoding="utf-8"))
            cur.execute(MIGRATION_0003.read_text(encoding="utf-8"))
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
        # load-bearing: graph_builder's article_parts INSERT uses
        # ON CONFLICT (container_id, part_id) DO NOTHING, which errors
        # outright without a matching unique index.
        cur.execute("SELECT to_regclass('episteme.article_parts_uq')")
        assert cur.fetchone()[0] is not None


def test_migration_0002_bookshelf_partition(sa_conn):
    with sa_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('episteme.articles_bookshelf')")
        assert cur.fetchone()[0] is not None
        cur.execute("SELECT to_regclass('episteme.articles_bookshelf_y0')")
        assert cur.fetchone()[0] is not None


def test_migration_0003_mesh_hierarchy_table(sa_conn):
    with sa_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('episteme.mesh_hierarchy')")
        assert cur.fetchone()[0] is not None
        cur.execute("SELECT to_regclass('episteme.mesh_hierarchy_uq')")
        assert cur.fetchone()[0] is not None
