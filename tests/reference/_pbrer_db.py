"""Fixture rows for the PBRER lookup tests (pg). Plain helpers; the fixtures are in
``conftest.py`` and are not autouse."""

from __future__ import annotations

import json
from pathlib import Path

from psycopg import sql

from episteme.data.regulatory.load_reg import partition_names

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_DIR = REPO_ROOT / "src/episteme/data/db"
MIGRATIONS = DB_DIR / "migrations"
# The AACT schema the tests use instead of the real ``aact`` views.
TEST_AACT_SCHEMA = "aact_lookup_test"
KW = {"tenant": "t", "caller": "test"}


def build_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute((DB_DIR / "extensions.sql").read_text("utf-8"))
        cur.execute((DB_DIR / "schema.sql").read_text("utf-8"))
        cur.execute((MIGRATIONS / "0002_container_and_book_parts.sql").read_text("utf-8"))
        cur.execute((MIGRATIONS / "0010_regulatory_sources.sql").read_text("utf-8"))
    conn.commit()


def clear(conn) -> None:
    """Empty every table the tests fill, drop the reg_record partitions and the test
    AACT schema."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = 'episteme.reg_record'::regclass"
        )
        for (name,) in cur.fetchall():
            cur.execute(sql.SQL("DROP TABLE {} CASCADE").format(sql.Identifier("episteme", name)))
        cur.execute(
            "TRUNCATE episteme.reg_release, episteme.label_section, episteme.label_substance, "
            "episteme.faers_moiety_quarter, episteme.faers_quarter_totals, "
            "episteme.article_mesh, episteme.articles, "
            "episteme.substance_xref, episteme.substance RESTART IDENTITY"
        )
        cur.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(TEST_AACT_SCHEMA))
        )
    conn.commit()


def add_substances(conn) -> None:
    """1 metformin (moiety), 2 metformin hydrochloride (moiety 1), 3 aspirin."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.substance (substance_key, unii, preferred_name, moiety_key) "
            "VALUES (1, 'UNIIMET01', 'Metformin', 1), "
            "(2, 'UNIIMET02', 'Metformin Hydrochloride', 1), (3, 'UNIIASP03', 'Aspirin', 3)"
        )
        cur.execute(
            "INSERT INTO episteme.substance_xref "
            "(substance_key, id_type, id_value, method, confidence, source) VALUES "
            "(1, 'name', 'metformin', 'normalised_name', 1.0, 'fda_substance'), "
            "(1, 'name', 'dimethylbiguanide', 'normalised_name', 1.0, 'fda_substance'), "
            "(2, 'name', 'metformin hydrochloride', 'normalised_name', 1.0, 'fda_substance'), "
            "(3, 'name', 'aspirin', 'normalised_name', 1.0, 'fda_substance'), "
            "(1, 'unii', 'UNIIMET01', 'exact_code', 1.0, 'fda_substance')"
        )
    conn.commit()


def add_release(conn, dataset: str, release_id: str, rows: list[dict], *, latest: bool) -> None:
    """A reg_record release partition with ``rows`` (record_key, substance_names,
    event_date, payload; jurisdiction optional) and its reg_release row."""
    ds_name, leaf = partition_names(dataset, release_id)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "CREATE TABLE IF NOT EXISTS {ds} PARTITION OF episteme.reg_record "
                "FOR VALUES IN ({v}) PARTITION BY LIST (release_id)"
            ).format(ds=sql.Identifier("episteme", ds_name), v=sql.Literal(dataset))
        )
        cur.execute(
            sql.SQL("CREATE TABLE {leaf} PARTITION OF {ds} FOR VALUES IN ({v})").format(
                leaf=sql.Identifier("episteme", leaf),
                ds=sql.Identifier("episteme", ds_name),
                v=sql.Literal(release_id),
            )
        )
        for r in rows:
            cur.execute(
                "INSERT INTO episteme.reg_record (dataset, release_id, record_key, jurisdiction, "
                "substance_names, event_date, payload, source_file) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
                (
                    dataset,
                    release_id,
                    r["record_key"],
                    r.get("jurisdiction", "US"),
                    r["substance_names"],
                    r.get("event_date"),
                    json.dumps(r.get("payload", {})),
                    f"{dataset}/{release_id}.json",
                ),
            )
        cur.execute(
            "INSERT INTO episteme.reg_release (dataset, release_id, rows, status, is_latest) "
            "VALUES (%s, %s, %s, 'loaded', %s)",
            (dataset, release_id, len(rows), latest),
        )
    conn.commit()
