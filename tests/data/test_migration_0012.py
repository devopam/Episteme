"""Migration 0012: openalex LIST partitions and the pubmed / pmc lower(doi) indexes
(pg: episteme_test only; drops and rebuilds the episteme schema there)."""

import psycopg
import pytest

from ._graph_schema import MIG, REPO

pytestmark = pytest.mark.pg

MIGRATION_0012 = MIG / "0012_openalex_partitions.sql"


def _base_schema(conn) -> None:
    """schema.sql + 0002-0011 (0001 needs pgvector), without 0012."""
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute((REPO / "src/episteme/data/db/extensions.sql").read_text(encoding="utf-8"))
        cur.execute((REPO / "src/episteme/data/db/schema.sql").read_text(encoding="utf-8"))
        for name in sorted(p.name for p in MIG.glob("00*.sql")):
            if name[:4] not in ("0001", "0012"):
                cur.execute((MIG / name).read_text(encoding="utf-8"))
    conn.commit()


def _insert(cur, source: str, n: int) -> None:
    cur.execute(
        "INSERT INTO episteme.articles (id, source, source_record_id, year, license, "
        "source_file) VALUES (%s, %s, %s, 0, 'open_metadata', 'f.json')",
        (f"{source}:{n}", source, str(n)),
    )
    cur.execute(
        "INSERT INTO episteme.article_body (article_id, source, text) VALUES (%s, %s, 'x')",
        (f"{source}:{n}", source),
    )


def _partition_of(cur, table: str, row_id: str, col: str) -> str:
    cur.execute(
        f"SELECT tableoid::regclass::text FROM episteme.{table} WHERE {col} = %s", (row_id,)
    )
    return cur.fetchone()[0]


def test_0012_refuses_while_the_default_partition_holds_an_openalex_row(pg_conn):
    """The documented precondition: purge first, or the migration stops with a message."""
    _base_schema(pg_conn)
    with pg_conn.cursor() as cur:
        _insert(cur, "openalex", 1)
        pg_conn.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="holds openalex rows"):
            cur.execute(MIGRATION_0012.read_text(encoding="utf-8"))
    pg_conn.rollback()
    # leave no openalex row behind: a later apply of 0012 would refuse
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM episteme.article_body WHERE source = 'openalex'")
        cur.execute("DELETE FROM episteme.articles WHERE source = 'openalex'")
    pg_conn.commit()


def test_0012_routes_openalex_to_its_own_partitions(pg_conn):
    _base_schema(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(MIGRATION_0012.read_text(encoding="utf-8"))
        cur.execute(MIGRATION_0012.read_text(encoding="utf-8"))  # idempotent
    # Committed: the last test of the file leaves episteme_test with 0002-0012.
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        for source in ("openalex", "chembl"):
            _insert(cur, source, 1)
        assert _partition_of(cur, "articles", "openalex:1", "id") == "episteme.articles_openalex"
        assert _partition_of(cur, "articles", "chembl:1", "id") == "episteme.articles_default"
        assert (
            _partition_of(cur, "article_body", "openalex:1", "article_id")
            == "episteme.article_body_openalex"
        )
        # the parent's (source, source_file) index cascades to the new partition
        cur.execute(
            "SELECT count(*) FROM pg_indexes WHERE schemaname = 'episteme' "
            "AND tablename = 'articles_openalex' AND indexdef LIKE '%(source, source_file)%'"
        )
        assert cur.fetchone()[0] == 1
        # lower(doi) only on pubmed and pmc (and their year partitions), not on the parent
        cur.execute(
            "SELECT tablename FROM pg_indexes WHERE schemaname = 'episteme' "
            "AND indexdef LIKE '%lower(doi)%'"
        )
        tables = {r[0] for r in cur.fetchall()}
        assert {"articles_pubmed", "articles_pmc"} <= tables
        assert all(t.startswith(("articles_pubmed", "articles_pmc")) for t in tables), tables
    pg_conn.rollback()
