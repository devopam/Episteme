import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.pg


def _setup_schema(conn):
    with (
        conn.cursor() as cur,
        open("src/episteme/data/db/extensions.sql") as ext,
        open("src/episteme/data/db/schema.sql") as sch,
    ):
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute(ext.read())
        cur.execute(sch.read())
    conn.commit()


def test_load_and_idempotent_replace(pg_conn, tmp_path, monkeypatch):
    monkeypatch.setenv("EPISTEME_ACTOR", "t")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    import episteme.config as cfg

    importlib.reload(cfg)
    cfg.get_settings.cache_clear()

    _setup_schema(pg_conn)

    from episteme.data import postgres_loader
    from episteme.data.article_schema import empty_article_row, finalize_row
    from episteme.data.staging_writer import write_rows

    rows = [
        finalize_row(
            {
                **empty_article_row(),
                "id": "pmcid:PMC1",
                "source": "pmc",
                "source_file": "B01.json",
                "pmcid": "PMC1",
                "year": 2024,
                "authors": ["Smith J", "Doe A"],
                "abstract": "an abstract about acetylcholinesterase " * 5,
                "license": "CC BY",
                "subset": "commercial",
            }
        ),
        finalize_row(
            {
                **empty_article_row(),
                "id": "pmcid:PMC2",
                "source": "pmc",
                "source_file": "B01.json",
                "pmcid": "PMC2",
                "year": None,  # -> coerced to 0 (articles_pmc_y0) before COPY
                "abstract": "second abstract about acetylcholinesterase " * 5,
                "license": "CC BY",
                "subset": "commercial",
            }
        ),
    ]
    write_info = write_rows(rows, tmp_path / "02_processed", source="pmc", source_file="B01.json")
    shard = Path(write_info["paths"][0])

    r1 = postgres_loader.load_source_file(pg_conn, source="pmc", staging_path=shard, run_id="r1")
    pg_conn.commit()
    assert r1["event"] == "load_commit"
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.articles WHERE source_file='B01.json'")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT text FROM episteme.article_body WHERE article_id='pmcid:PMC1'")
        assert "acetylcholinesterase" in cur.fetchone()[0]
        # text[] round-trips as a Python list through COPY
        cur.execute("SELECT authors FROM episteme.articles WHERE pmcid='PMC1'")
        assert cur.fetchone()[0] == ["Smith J", "Doe A"]
        # year None -> 0, and the row lands in the y0 sub-partition
        cur.execute("SELECT year FROM episteme.articles WHERE pmcid='PMC2'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM episteme.articles_pmc_y0 WHERE pmcid='PMC2'")
        assert cur.fetchone()[0] == 1

    # re-load same source_file -> replace, still 2 rows, audit has a load_replace
    r2 = postgres_loader.load_source_file(pg_conn, source="pmc", staging_path=shard, run_id="r2")
    pg_conn.commit()
    assert r2["event"] == "load_replace"
    assert r2["deleted"] == 2
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.articles WHERE source_file='B01.json'")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT count(*) FROM episteme.article_body WHERE article_id='pmcid:PMC1'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM episteme._audit WHERE event_type='load_replace'")
        assert cur.fetchone()[0] >= 1
        cur.execute("SELECT count(*) FROM episteme._lineage WHERE source_file='B01.json'")
        assert cur.fetchone()[0] == 2

    # shrinking re-load: the shard now carries only PMC1. PMC2's articles row
    # (deleted by source_file) AND its article_body row (which a naive id-keyed
    # delete would orphan) must both be gone.
    shrunk = write_rows([rows[0]], tmp_path / "02_processed", source="pmc", source_file="B01.json")
    postgres_loader.load_source_file(
        pg_conn, source="pmc", staging_path=Path(shrunk["paths"][0]), run_id="r3"
    )
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.articles WHERE source_file='B01.json'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM episteme.article_body WHERE article_id='pmcid:PMC2'")
        assert cur.fetchone()[0] == 0  # no orphan
