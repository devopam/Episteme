"""bulk_indexes without a database: definition rewriting and the guard."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from episteme.data.db import bulk_indexes, connection

PARENT_DEFS = {
    "articles_pmid_idx": (
        "CREATE INDEX articles_pmid_idx ON ONLY episteme.articles "
        "USING btree (pmid) WHERE (pmid IS NOT NULL)"
    ),
    "articles_mesh_gin": (
        "CREATE INDEX articles_mesh_gin ON ONLY episteme.articles USING gin (mesh)"
    ),
}


@pytest.mark.parametrize("name", sorted(PARENT_DEFS))
def test_rebuild_sql_builds_on_every_partition(name):
    out = bulk_indexes.rebuild_sql(PARENT_DEFS[name], name)
    assert out == PARENT_DEFS[name].replace(" ON ONLY ", " ON ", 1)
    assert "ON ONLY" not in out


@pytest.mark.parametrize(
    "bad",
    [
        "CREATE INDEX x ON episteme.articles USING btree (pmid)",
        "CREATE UNIQUE INDEX x ON ONLY episteme.articles USING btree (pmid)",
        "CREATE INDEX x ON ONLY episteme.other USING btree (pmid)",
        "DROP TABLE episteme.articles; CREATE INDEX x ON ONLY episteme.articles USING gin (mesh)",
        "",
    ],
)
def test_rebuild_sql_refuses_anything_else(bad):
    with pytest.raises(bulk_indexes.BulkIndexError):
        bulk_indexes.rebuild_sql(bad, "x")


def test_rebuild_sql_refuses_a_trailing_statement():
    bad = PARENT_DEFS["articles_mesh_gin"] + "; DROP TABLE episteme.articles"
    with pytest.raises(bulk_indexes.BulkIndexError):
        bulk_indexes.rebuild_sql(bad, "articles_mesh_gin")


def test_rebuild_sql_refuses_a_name_mismatch():
    with pytest.raises(bulk_indexes.BulkIndexError, match="expected"):
        bulk_indexes.rebuild_sql(PARENT_DEFS["articles_mesh_gin"], "articles_pmid_idx")


def test_covered_indexes_are_exactly_the_six():
    assert bulk_indexes.COVERED_INDEXES == (
        "articles_pmid_idx",
        "articles_pmcid_idx",
        "articles_doi_idx",
        "articles_mesh_gin",
        "articles_authors_gin",
        "articles_publication_types_gin",
    )


def test_main_refuses_under_the_guard_without_connecting(monkeypatch, capsys):
    refused = SimpleNamespace(
        db_mode="restricted",
        pg_database="episteme",
        production_database="episteme",
        pg_host="localhost",
        pg_port=5433,
        sys_admin_password="x",
    )
    monkeypatch.setattr(
        connection, "admin_dsn_from_settings", lambda: connection.admin_dsn(refused)
    )

    def _no_connect(*_a, **_k):
        raise AssertionError("connected despite the guard")

    monkeypatch.setattr(bulk_indexes.psycopg, "connect", _no_connect)
    assert bulk_indexes.main(["drop"]) == 1
    assert "Refusing" in capsys.readouterr().err


def test_main_rejects_an_unknown_action():
    with pytest.raises(SystemExit) as exc:
        bulk_indexes.main(["vacuum"])
    assert exc.value.code == 2


def test_main_passes_pool_kwargs_and_status_error_exits_3(monkeypatch):
    import psycopg

    monkeypatch.setattr(
        bulk_indexes,
        "get_settings",
        lambda: SimpleNamespace(db_mode="read-only", run_id=None),
    )
    monkeypatch.setattr(connection, "admin_dsn_from_settings", lambda: "dbname=dummy")
    captured = {}

    def _connect(dsn, **kwargs):
        captured.update(kwargs)
        raise psycopg.OperationalError("stop")

    monkeypatch.setattr(bulk_indexes.psycopg, "connect", _connect)
    assert bulk_indexes.main(["status"]) == 3
    assert captured["options"] == "-c default_transaction_read_only=on"
