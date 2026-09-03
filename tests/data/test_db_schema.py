import pytest

pytestmark = pytest.mark.pg


def test_schema_and_partitions(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = 'episteme'")
        assert cur.fetchone()

        cur.execute(
            """
            SELECT count(*)
              FROM pg_partitioned_table pt
              JOIN pg_class c ON c.oid = pt.partrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'episteme'
            """
        )
        # articles, articles_pmc, article_body, article_cites, article_mesh,
        # chunks, _audit -> at least 5 partitioned parents.
        assert cur.fetchone()[0] >= 5

        cur.execute("SELECT has_table_privilege('episteme_app', 'episteme._audit', 'UPDATE')")
        assert cur.fetchone()[0] is False

        cur.execute("SELECT has_table_privilege('episteme_app', 'episteme._audit', 'DELETE')")
        assert cur.fetchone()[0] is False

        cur.execute("SELECT has_table_privilege('episteme_app', 'episteme._audit', 'INSERT')")
        assert cur.fetchone()[0] is True
