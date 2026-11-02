"""Migration 0010: reference-feed, substance, FAERS and label tables.

pg-marked: runs against ``episteme_test`` only (the ``pg_conn`` fixture refuses
any other database).
"""

from pathlib import Path

import psycopg
import pytest

pytestmark = pytest.mark.pg

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "src/episteme/data/db/migrations"
MIGRATION_0002 = MIGRATIONS / "0002_container_and_book_parts.sql"
MIGRATION_0010 = MIGRATIONS / "0010_regulatory_sources.sql"

NEW_TABLES = (
    "reg_release",
    "reg_record",
    "substance",
    "substance_xref",
    "faers_report",
    "faers_drug",
    "faers_reaction",
    "faers_drug_entry",
    "faers_entry_substance",
    "faers_term",
    "faers_pair_quarter",
    "faers_event_quarter",
    "faers_quarter_totals",
    "faers_batch_coverage",
    "faers_batch_quarter",
    "faers_moiety_quarter",
    "faers_term_quarter",
    "recall_lot",
    "label_section",
    "label_substance",
    "articles_dailymed",
    "articles_ema_epar",
    "article_body_dailymed",
    "article_body_ema_epar",
)


def _fresh_schema(conn):
    with (
        conn.cursor() as cur,
        open("src/episteme/data/db/extensions.sql") as ext,
        open("src/episteme/data/db/schema.sql") as sch,
    ):
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute(ext.read())
        cur.execute(sch.read())
        cur.execute(MIGRATION_0002.read_text(encoding="utf-8"))
        cur.execute(MIGRATION_0010.read_text(encoding="utf-8"))
    conn.commit()


@pytest.fixture
def db(pg_conn):
    _fresh_schema(pg_conn)
    return pg_conn


def _explain(conn, sql: str) -> str:
    with conn.cursor() as cur:
        cur.execute("EXPLAIN " + sql)
        return "\n".join(r[0] for r in cur.fetchall())


def test_migration_0010_is_idempotent(db):
    with db.cursor() as cur:
        cur.execute(MIGRATION_0010.read_text(encoding="utf-8"))
        cur.execute(MIGRATION_0010.read_text(encoding="utf-8"))
    db.commit()
    with db.cursor() as cur:
        for name in NEW_TABLES:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"episteme.{name}",))
            assert cur.fetchone()[0], name


def test_only_one_latest_release_per_dataset(db):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.reg_release (dataset, release_id, rows, status, is_latest) "
            "VALUES ('ema_orphan', '2026-10', 1, 'loaded', true), "
            "('ema_orphan', '2026-09', 1, 'loaded', false), "
            "('ema_shortage', '2026-10', 1, 'loaded', true)"
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO episteme.reg_release (dataset, release_id, rows, status, is_latest) "
                "VALUES ('ema_orphan', '2026-11', 1, 'loaded', true)"
            )


def test_latest_release_prunes_to_one_partition(db):
    with db.cursor() as cur:
        for ds in ("ema_orphan", "ema_shortage"):
            cur.execute(
                f"CREATE TABLE episteme.reg_record_{ds} PARTITION OF episteme.reg_record "
                f"FOR VALUES IN ('{ds}') PARTITION BY LIST (release_id)"
            )
            for rel in ("r1", "r2"):
                cur.execute(
                    f"CREATE TABLE episteme.reg_record_{ds}_{rel} "
                    f"PARTITION OF episteme.reg_record_{ds} FOR VALUES IN ('{rel}')"
                )
    plan = _explain(
        db,
        "SELECT * FROM episteme.reg_record "
        "WHERE dataset = 'ema_orphan' AND release_id = 'r2' AND 'x' = ANY (substance_names)",
    )
    assert "reg_record_ema_orphan_r2" in plan
    for other in ("reg_record_ema_orphan_r1", "reg_record_ema_shortage"):
        assert other not in plan


def test_faers_year_prunes(db):
    plan = _explain(db, "SELECT * FROM episteme.faers_drug WHERE receive_year >= 2024")
    for year in range(2024, 2031):
        assert f"faers_drug_y{year}" in plan
    for year in (2004, 2010, 2023):
        assert f"faers_drug_y{year}" not in plan


def test_faers_partitions_cover_2004_to_2030_plus_default(db):
    with db.cursor() as cur:
        for parent in ("faers_report", "faers_drug", "faers_reaction"):
            cur.execute(
                "SELECT count(*) FROM pg_inherits i JOIN pg_class p ON p.oid = i.inhparent "
                "JOIN pg_namespace n ON n.oid = p.relnamespace "
                "WHERE n.nspname = 'episteme' AND p.relname = %s",
                (parent,),
            )
            assert cur.fetchone()[0] == 27 + 1, parent
        # a report with no usable date has no receive_year and lands in the default
        cur.execute("INSERT INTO episteme.faers_report (safetyreportid) VALUES ('x')")
        cur.execute("SELECT tableoid::regclass::text FROM episteme.faers_report")
        assert cur.fetchone()[0] == "episteme.faers_report_default"


def test_faers_parents_hold_no_index(db):
    with db.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_indexes WHERE schemaname = 'episteme' "
            "AND tablename IN ('faers_report', 'faers_drug', 'faers_reaction')"
        )
        assert cur.fetchall() == []


def test_faers_drug_carries_both_batch_columns(db):
    with db.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'episteme' "
            "AND table_name = 'faers_drug' AND column_name IN ('batch_number', 'batch_norm')"
        )
        assert {r[0] for r in cur.fetchall()} == {"batch_number", "batch_norm"}


def test_text_sources_have_their_own_partitions(db):
    with db.cursor() as cur:
        cur.execute("INSERT INTO episteme.articles (id, source) VALUES ('dailymed:1', 'dailymed')")
        cur.execute("SELECT tableoid::regclass::text FROM episteme.articles")
        assert cur.fetchone()[0] == "episteme.articles_dailymed"
