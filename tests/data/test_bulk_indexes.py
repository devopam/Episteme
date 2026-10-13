"""bulk_indexes against episteme_test: drop, rebuild, refusal and resume."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.pg

REPO_ROOT = Path(__file__).resolve().parents[2]
MIG = REPO_ROOT / "src" / "episteme" / "data" / "db" / "migrations"
COLUMN_MARKERS = ("(pmid)", "(pmcid)", "(doi)", "(mesh)", "(authors)", "(publication_types)")
SNAPSHOT_SQL = (
    "SELECT c.relname, pg_get_indexdef(c.oid), i.indisvalid "
    "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "JOIN pg_class t ON t.oid = i.indrelid JOIN pg_namespace n ON n.oid = t.relnamespace "
    "WHERE n.nspname = 'episteme' AND (t.relname = 'articles' OR t.relname LIKE 'articles\\_%') "
    "ORDER BY c.relname"
)


@pytest.fixture
def bulk(pg_conn, monkeypatch, tmp_path):
    monkeypatch.setenv("EPISTEME_ACTOR", "test-actor")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    import episteme.config as cfg

    importlib.reload(cfg)
    cfg.get_settings.cache_clear()
    from episteme import audit_trail

    importlib.reload(audit_trail)
    from episteme.data.db import bulk_indexes

    importlib.reload(bulk_indexes)
    with (
        pg_conn.cursor() as cur,
        open(REPO_ROOT / "src/episteme/data/db/extensions.sql") as ext,
        open(REPO_ROOT / "src/episteme/data/db/schema.sql") as sch,
    ):
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute(ext.read())
        cur.execute(sch.read())
        for m in (
            "0002_container_and_book_parts",
            "0004_articles_load_indexes",
            "0005_bulk_index_state",
        ):
            cur.execute((MIG / f"{m}.sql").read_text(encoding="utf-8"))
    pg_conn.commit()
    return bulk_indexes, audit_trail


def _snapshot(conn):
    with conn.cursor() as cur:
        cur.execute(SNAPSHOT_SQL)
        rows = cur.fetchall()
    conn.commit()
    return rows


def _pending(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT index_name FROM episteme._bulk_index_state ORDER BY index_name")
        rows = [r[0] for r in cur.fetchall()]
    conn.commit()
    return rows


def _events(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT event_type FROM episteme._audit ORDER BY seq")
        rows = [r[0] for r in cur.fetchall()]
    conn.commit()
    return rows


def _set_definition(conn, index_name: str, definition: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE episteme._bulk_index_state SET definition = %s WHERE index_name = %s",
            (definition, index_name),
        )
    conn.commit()


def _definition(conn, index_name: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT definition FROM episteme._bulk_index_state WHERE index_name = %s",
            (index_name,),
        )
        (d,) = cur.fetchone()
    conn.commit()
    return d


def test_drop_then_rebuild_restores_every_index_exactly(pg_conn, bulk):
    bi, audit_trail = bulk
    before = _snapshot(pg_conn)
    assert all(valid for _, _, valid in before)

    assert bi.drop(pg_conn, run_id="r1") == list(bi.COVERED_INDEXES)
    after_drop = _snapshot(pg_conn)
    assert not [d for _, d, _ in after_drop if any(m in d for m in COLUMN_MARKERS)]
    assert len(_pending(pg_conn)) == 6

    assert len(bi.rebuild(pg_conn, run_id="r1")) == 6
    assert _snapshot(pg_conn) == before
    assert _pending(pg_conn) == []

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_inherits WHERE inhparent = 'episteme.articles'::regclass"
        )
        (direct_partitions,) = cur.fetchone()
        cur.execute(
            "SELECT c.relname, count(inh.inhrelid) FROM pg_class c "
            "LEFT JOIN pg_inherits inh ON inh.inhparent = c.oid "
            "WHERE c.relnamespace = 'episteme'::regnamespace AND c.relname = ANY(%s) "
            "GROUP BY c.relname",
            (list(bi.COVERED_INDEXES),),
        )
        attached = dict(cur.fetchall())
    pg_conn.commit()
    assert set(attached) == set(bi.COVERED_INDEXES)
    assert all(n == direct_partitions for n in attached.values())

    assert _events(pg_conn) == ["bulk_index_drop", "bulk_index_rebuild"]
    assert audit_trail.verify(pg_conn) == []


def test_drop_refuses_when_a_rebuild_is_pending(pg_conn, bulk):
    bi, _ = bulk
    bi.drop(pg_conn, run_id="r1")
    after_first = _snapshot(pg_conn)
    with pytest.raises(bi.PendingRebuildError):
        bi.drop(pg_conn, run_id="r2")
    assert _snapshot(pg_conn) == after_first
    assert len(_pending(pg_conn)) == 6
    assert _events(pg_conn) == ["bulk_index_drop"]


def test_drop_refuses_when_a_covered_index_is_missing(pg_conn, bulk):
    bi, _ = bulk
    with pg_conn.cursor() as cur:
        cur.execute("DROP INDEX episteme.articles_doi_idx")
    pg_conn.commit()
    before = _snapshot(pg_conn)
    with pytest.raises(bi.BulkIndexError, match="articles_doi_idx"):
        bi.drop(pg_conn, run_id="r1")
    assert _snapshot(pg_conn) == before
    assert _pending(pg_conn) == []


def test_rebuild_refuses_a_definition_without_on_only(pg_conn, bulk):
    bi, _ = bulk
    bi.drop(pg_conn, run_id="r1")
    name = "episteme.articles_doi_idx"
    _set_definition(pg_conn, name, _definition(pg_conn, name).replace(" ON ONLY ", " ON ", 1))
    with pytest.raises(bi.BulkIndexError):
        bi.rebuild(pg_conn, run_id="r1")
    assert len(_pending(pg_conn)) == 6  # validated up front: nothing rebuilt


def test_interrupted_rebuild_keeps_the_rest_and_resumes(pg_conn, bulk):
    import psycopg

    bi, _ = bulk
    before = _snapshot(pg_conn)
    bi.drop(pg_conn, run_id="r1")
    name = "episteme.articles_doi_idx"  # second in index_name order, after authors
    good = _definition(pg_conn, name)
    _set_definition(
        pg_conn,
        name,
        "CREATE INDEX articles_doi_idx ON ONLY episteme.articles USING btree (no_such_column)",
    )
    with pytest.raises(psycopg.Error):
        bi.rebuild(pg_conn, run_id="r1")
    pending = _pending(pg_conn)
    assert "episteme.articles_authors_gin" not in pending
    assert name in pending
    assert len(pending) == 5

    _set_definition(pg_conn, name, good)
    assert len(bi.rebuild(pg_conn, run_id="r1")) == 5
    assert _snapshot(pg_conn) == before
    assert _pending(pg_conn) == []


def test_rebuild_replaces_an_invalid_index_of_the_same_name(pg_conn, bulk):
    bi, _ = bulk
    before = _snapshot(pg_conn)
    bi.drop(pg_conn, run_id="r1")
    with pg_conn.cursor() as cur:
        # what an operator gets by running the saved text verbatim: an empty,
        # invalid parent index with no partition indexes
        cur.execute(_definition(pg_conn, "episteme.articles_pmid_idx"))
        cur.execute(
            "SELECT indisvalid FROM pg_index "
            "WHERE indexrelid = 'episteme.articles_pmid_idx'::regclass"
        )
        assert cur.fetchone()[0] is False
    pg_conn.commit()
    bi.rebuild(pg_conn, run_id="r1")
    assert _snapshot(pg_conn) == before


def test_rebuild_with_nothing_pending_is_a_no_op(pg_conn, bulk):
    bi, _ = bulk
    assert bi.rebuild(pg_conn, run_id="r1") == []
    assert _events(pg_conn) == []
