"""The corpus leaves out regulatory reference text (DailyMed labels, EMA EPARs).

The owner's design: publications train the model; regulatory reference data is looked up,
not trained on. The list is in config (``corpus_excluded_sources``). The read runs here on
an in-memory DuckDB that mimics the attached Postgres (``pg.episteme``): no database.
"""

from __future__ import annotations

import duckdb

from episteme.config import CORPUS_EXCLUDED_SOURCES, get_settings
from episteme.data import corpus_materializer as cm


def _db() -> duckdb.DuckDBPyConnection:
    d = duckdb.connect()
    d.execute("ATTACH ':memory:' AS pg")
    d.execute("CREATE SCHEMA pg.episteme")
    d.execute(
        "CREATE TABLE pg.episteme.articles (id TEXT, source TEXT, year INT, pmid TEXT, "
        "doi TEXT, license TEXT, content_hash TEXT, subset TEXT, extract_status TEXT)"
    )
    d.execute("CREATE TABLE pg.episteme.article_body (article_id TEXT, text TEXT)")
    for i, source in enumerate(("pmc", "dailymed", "ema_epar", "pubmed")):
        d.execute(
            "INSERT INTO pg.episteme.articles VALUES (?, ?, 2020, NULL, NULL, 'cc-by', ?, "
            "'commercial', 'ok')",
            [f"{source}:1", source, f"h{i}"],
        )
        d.execute("INSERT INTO pg.episteme.article_body VALUES (?, 'text')", [f"{source}:1"])
    return d


def test_the_config_excludes_dailymed_and_ema_epar():
    assert CORPUS_EXCLUDED_SOURCES == ("dailymed", "ema_epar")
    assert get_settings().corpus_excluded_sources == ("dailymed", "ema_epar")


def test_the_read_leaves_out_the_excluded_sources():
    d = _db()
    try:
        query, params = cm.select_sql(CORPUS_EXCLUDED_SOURCES)
        sources = sorted(row[1] for row in d.execute(query, params).fetchall())
    finally:
        d.close()
    assert sources == ["pmc", "pubmed"]


def test_an_empty_list_excludes_nothing():
    d = _db()
    try:
        query, params = cm.select_sql(())
        assert params == [] and "NOT IN" not in query
        assert len(d.execute(query, params).fetchall()) == 4
    finally:
        d.close()


def test_materialize_reads_the_list_from_config(monkeypatch):
    """``materialize`` takes the config list when the caller gives none."""
    seen = {}

    class Stop(Exception):
        pass

    def fake_select(excluded):
        seen["excluded"] = excluded
        raise Stop

    monkeypatch.setattr(cm, "select_sql", fake_select)
    monkeypatch.setattr(cm, "build_test_ngrams", lambda *a, **k: set())
    try:
        cm.materialize(None, out_root="x", run_id="r", pg_dsn="dsn")
    except Stop:
        pass
    assert seen["excluded"] == ("dailymed", "ema_epar")
