"""pg-marked tests for postgres_loader.retire_source_files (SP7 Task 4).

Follows tests/data/test_postgres_loader.py's / tests/data/test_audit_trail.py's
pg_conn + _setup_schema pattern: DROP SCHEMA + reapply extensions.sql +
schema.sql on the fixture connection, then explicit INSERTs and
SELECT count(*) assertions (no ORM, no fixtures beyond raw SQL).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.pg

REPO_ROOT = Path(__file__).resolve().parents[2]

OLD_SF = "SDTM__2026-06-26__SDTM_Terminology.txt"
NEW_SF = "SDTM__2026-09-25__SDTM_Terminology.txt"


def _setup_schema(conn):
    with (
        conn.cursor() as cur,
        open(REPO_ROOT / "src/episteme/data/db/extensions.sql") as ext,
        open(REPO_ROOT / "src/episteme/data/db/schema.sql") as sch,
    ):
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute(ext.read())
        cur.execute(sch.read())
    conn.commit()


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("EPISTEME_ACTOR", "t")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    import episteme.config as cfg

    importlib.reload(cfg)
    cfg.get_settings.cache_clear()
    from episteme import audit_trail

    importlib.reload(audit_trail)


def _insert_article(conn, *, id_, source, source_file):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.articles (id, source, source_file) VALUES (%s, %s, %s)",
            (id_, source, source_file),
        )


def _insert_body(conn, *, article_id, source, text="body text"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.article_body (article_id, source, text) VALUES (%s, %s, %s)",
            (article_id, source, text),
        )


def test_retires_old_release_keeps_new_and_other_sources(pg_conn, monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    _setup_schema(pg_conn)
    from episteme.data import postgres_loader

    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1", source="cdisc_ct", source_file=OLD_SF)
    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C2:p1", source="cdisc_ct", source_file=OLD_SF)
    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1:v2", source="cdisc_ct", source_file=NEW_SF)
    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C2:p1:v2", source="cdisc_ct", source_file=NEW_SF)
    _insert_article(pg_conn, id_="mesh:D000001", source="mesh", source_file="desc2026.xml")
    pg_conn.commit()

    deleted = postgres_loader.retire_source_files(
        pg_conn, source="cdisc_ct", keep_source_files=[NEW_SF], run_id="r1"
    )
    assert deleted == 2

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM episteme.articles WHERE source='cdisc_ct' AND source_file=%s",
            (OLD_SF,),
        )
        assert cur.fetchone()[0] == 0

        cur.execute(
            "SELECT count(*) FROM episteme.articles WHERE source='cdisc_ct' AND source_file=%s",
            (NEW_SF,),
        )
        assert cur.fetchone()[0] == 2

        cur.execute("SELECT count(*) FROM episteme.articles WHERE source='mesh'")
        assert cur.fetchone()[0] == 1


def test_article_body_follows(pg_conn, monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    _setup_schema(pg_conn)
    from episteme.data import postgres_loader

    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1", source="cdisc_ct", source_file=OLD_SF)
    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C2:p1", source="cdisc_ct", source_file=NEW_SF)
    # a mesh row reusing the SAME article_id as a retired cdisc_ct row, to prove
    # the article_body delete is scoped by source (never touches another source).
    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1", source="mesh", source_file="desc2026.xml")
    pg_conn.commit()

    _insert_body(pg_conn, article_id="cdisc_ct:SDTM:C1:p1", source="cdisc_ct")
    _insert_body(pg_conn, article_id="cdisc_ct:SDTM:C2:p1", source="cdisc_ct")
    _insert_body(pg_conn, article_id="cdisc_ct:SDTM:C1:p1", source="mesh")
    pg_conn.commit()

    deleted = postgres_loader.retire_source_files(
        pg_conn, source="cdisc_ct", keep_source_files=[NEW_SF], run_id="r1"
    )
    assert deleted == 1

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM episteme.article_body "
            "WHERE article_id='cdisc_ct:SDTM:C1:p1' AND source='cdisc_ct'"
        )
        assert cur.fetchone()[0] == 0

        cur.execute(
            "SELECT count(*) FROM episteme.article_body "
            "WHERE article_id='cdisc_ct:SDTM:C2:p1' AND source='cdisc_ct'"
        )
        assert cur.fetchone()[0] == 1

        # the mesh row sharing the retired article_id must survive untouched.
        cur.execute(
            "SELECT count(*) FROM episteme.article_body "
            "WHERE article_id='cdisc_ct:SDTM:C1:p1' AND source='mesh'"
        )
        assert cur.fetchone()[0] == 1


def test_records_load_replace_audit(pg_conn, monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    _setup_schema(pg_conn)
    from episteme.data import postgres_loader

    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1", source="cdisc_ct", source_file=OLD_SF)
    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1:v2", source="cdisc_ct", source_file=NEW_SF)
    pg_conn.commit()

    deleted = postgres_loader.retire_source_files(
        pg_conn, source="cdisc_ct", keep_source_files=[NEW_SF], run_id="r1"
    )
    assert deleted == 1

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT object, rows_affected, run_id FROM episteme._audit "
            "WHERE event_type='load_replace' ORDER BY seq DESC LIMIT 1"
        )
        object_, rows_affected, run_id = cur.fetchone()
        assert object_ == "cdisc_ct retire"
        assert rows_affected == 1
        assert run_id == "r1"


def test_empty_keep_set_raises_and_deletes_nothing(pg_conn, monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    _setup_schema(pg_conn)
    from episteme.data import postgres_loader

    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1", source="cdisc_ct", source_file=OLD_SF)
    _insert_article(pg_conn, id_="cdisc_ct:SDTM:C1:p1:v2", source="cdisc_ct", source_file=NEW_SF)
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme._audit")
        audit_before = cur.fetchone()[0]

    with pytest.raises(ValueError):
        postgres_loader.retire_source_files(
            pg_conn, source="cdisc_ct", keep_source_files=[], run_id="r1"
        )

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.articles WHERE source='cdisc_ct'")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT count(*) FROM episteme._audit")
        assert cur.fetchone()[0] == audit_before
