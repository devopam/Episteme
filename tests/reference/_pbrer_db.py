"""Fixture rows for the PBRER lookup tests (pg). Plain helpers; the fixtures are in
``conftest.py`` and are not autouse."""

from __future__ import annotations

import json
from pathlib import Path

from psycopg import sql

from episteme.data.regulatory.load_reg import partition_names
from episteme.reference import api_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_DIR = REPO_ROOT / "src/episteme/data/db"
MIGRATIONS = DB_DIR / "migrations"
# The AACT release the tests load, and its release schema (load_aact.schema_name). The
# trials function reads ``aact_r<release_id>`` of the latest ``aact`` release. The id is
# one no real release has: ``clear`` drops this schema.
TEST_AACT_RELEASE = "19700101"
TEST_AACT_SCHEMA = f"aact_r{TEST_AACT_RELEASE}"
KW = {"tenant": "t", "caller": "test"}
# Migrations applied after schema.sql, in order: the tables and columns the lookups read
# (0015 also creates faers_moiety_quarter_outcome, after the 0010 FAERS tables).
_MIGRATION_FILES = (
    "0002_container_and_book_parts.sql",
    "0006_pubmed_graph.sql",
    "0010_regulatory_sources.sql",
    "0011_label_section_ordinal.sql",
    "0014_article_text_virtual.sql",
    "0015_api_layer.sql",
    "0016_api_perf.sql",
    "0017_api_no_timeouts.sql",
    "0018_question_terms.sql",
)


def build_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute((DB_DIR / "extensions.sql").read_text("utf-8"))
        cur.execute((DB_DIR / "schema.sql").read_text("utf-8"))
        # 0006: article_chemicals and mesh_tree (graph_neighbours, literature_by_mesh).
        # 0011: label_section.ordinal and product (current_label, label_section_text).
        # 0014 is a no-op on a schema.sql database (article_body already has
        # text_override and the virtual text). 0015 is in schema.sql too; applied again
        # (idempotent) so the API functions match the migration file; 0016 and 0017 then
        # replace the bodies they change (schema.sql carries the latest ones). 0017 also
        # creates mesh_qualifier before grant_owner runs.
        for name in _MIGRATION_FILES:
            cur.execute((MIGRATIONS / name).read_text("utf-8"))
    conn.commit()
    grant_owner(conn)


def grant_owner(conn) -> None:
    """The API owner role's reads (grants/0015_api_tables.sql, as ``api_settings sync``
    applies them). Once the grants file has run on the database, the functions run as
    episteme_api_owner, and a table or AACT schema created afterwards (a schema rebuild, a
    test AACT release) needs this, or the functions fail with "permission denied". A
    no-op when the role does not exist."""
    api_settings.apply_owner_grants(conn)
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
            "episteme.faers_moiety_quarter_outcome, episteme.faers_event_quarter, "
            "episteme.faers_term, "
            "episteme.article_mesh, episteme.article_cites, episteme.article_chemicals, "
            "episteme.articles, episteme.mesh_term, episteme.mesh_term_vec, "
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
