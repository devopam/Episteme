import psycopg
import pytest

from ._graph_schema import MIG, REPO

pytestmark = pytest.mark.pg

MIGRATION_0009 = MIG / "0009_apollo_pubchem_partitions.sql"


def _base_schema(conn) -> None:
    """schema.sql + 0002-0008, without 0009."""
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute((REPO / "src/episteme/data/db/extensions.sql").read_text(encoding="utf-8"))
        cur.execute((REPO / "src/episteme/data/db/schema.sql").read_text(encoding="utf-8"))
        for name in sorted(p.name for p in MIG.glob("000[2-8]_*.sql")):
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


def test_0009_routes_apollo_and_pubchem_to_their_own_partitions(pg_conn):
    _base_schema(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(MIGRATION_0009.read_text(encoding="utf-8"))
        cur.execute(MIGRATION_0009.read_text(encoding="utf-8"))  # idempotent
        for source in ("apollo", "pubchem", "chembl"):
            _insert(cur, source, 1)
        assert _partition_of(cur, "articles", "apollo:1", "id") == "episteme.articles_apollo"
        assert _partition_of(cur, "articles", "pubchem:1", "id") == "episteme.articles_pubchem"
        assert _partition_of(cur, "articles", "chembl:1", "id") == "episteme.articles_default"
        assert (
            _partition_of(cur, "article_body", "apollo:1", "article_id")
            == "episteme.article_body_apollo"
        )
        assert (
            _partition_of(cur, "article_body", "pubchem:1", "article_id")
            == "episteme.article_body_pubchem"
        )
        # the parent's (source, source_file) index cascades to the new partitions
        cur.execute(
            "SELECT count(*) FROM pg_indexes WHERE schemaname = 'episteme' "
            "AND tablename IN ('articles_apollo', 'articles_pubchem') "
            "AND indexdef LIKE '%(source, source_file)%'"
        )
        assert cur.fetchone()[0] == 2
    pg_conn.rollback()


def test_0009_refuses_while_the_default_partition_holds_an_apollo_row(pg_conn):
    """The documented precondition: purge first, or CREATE ... PARTITION OF fails."""
    _base_schema(pg_conn)
    with pg_conn.cursor() as cur:
        _insert(cur, "apollo", 1)
        pg_conn.commit()
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(MIGRATION_0009.read_text(encoding="utf-8"))
    pg_conn.rollback()
