from pathlib import Path

import pytest

pytestmark = pytest.mark.pg

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_0002 = REPO_ROOT / "src/episteme/data/db/migrations/0002_container_and_book_parts.sql"
MIGRATION_0004 = REPO_ROOT / "src/episteme/data/db/migrations/0004_articles_load_indexes.sql"


def _setup_schema(conn):
    with (
        conn.cursor() as cur,
        open("src/episteme/data/db/extensions.sql") as ext,
        open("src/episteme/data/db/schema.sql") as sch,
    ):
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute(ext.read())
        cur.execute(sch.read())
        cur.execute(MIGRATION_0002.read_text(encoding="utf-8"))
    conn.commit()


def test_migration_0004_creates_load_indexes_and_delete_uses_them(pg_conn):
    _setup_schema(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(MIGRATION_0004.read_text(encoding="utf-8"))
        cur.execute(MIGRATION_0004.read_text(encoding="utf-8"))  # idempotent
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT tablename, indexname FROM pg_indexes WHERE schemaname = 'episteme' "
            "AND tablename LIKE 'articles%%' AND (indexdef LIKE %s OR indexdef LIKE %s)",
            ("%(source, source_file)%", "%USING btree (id)%"),
        )
        found = cur.fetchall()
        tables = {t for t, _ in found}
        assert "articles" in tables
        assert "articles_default" in tables
        names = {i for _, i in found}
        assert "articles_source_file_idx" in names
        assert "articles_id_idx" in names

        # The table is empty, so the planner would pick a seq scan on cost
        # alone; disable it to assert the predicate is index-servable.
        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute(
            "EXPLAIN DELETE FROM episteme.articles "
            "WHERE source_file = ANY(ARRAY['f']) AND source = 'x'"
        )
        plan = "\n".join(r[0] for r in cur.fetchall())
    assert "articles_default" in plan
    assert "Index Scan" in plan or "Bitmap" in plan, plan
    assert "Seq Scan on" not in plan, plan
