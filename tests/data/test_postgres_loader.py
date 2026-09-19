import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.pg

REPO_ROOT = Path(__file__).resolve().parents[2]


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

    # a different source sharing the same source_file basename must never be
    # touched by a pmc load -- source_file basenames are not globally unique
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.articles (id, source, source_file) VALUES (%s, %s, %s)",
            ("pubmed:other", "pubmed", "B01.json"),
        )
    pg_conn.commit()

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
        cur.execute(
            "SELECT count(*) FROM episteme.articles WHERE source_file='B01.json' AND source='pmc'"
        )
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
        cur.execute(
            "SELECT count(*) FROM episteme.articles WHERE source_file='B01.json' AND source='pmc'"
        )
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
        cur.execute(
            "SELECT count(*) FROM episteme.articles WHERE source_file='B01.json' AND source='pmc'"
        )
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM episteme.article_body WHERE article_id='pmcid:PMC2'")
        assert cur.fetchone()[0] == 0  # no orphan

    # the pubmed row inserted above, sharing B01.json's source_file, must
    # still be untouched by all the pmc loads/reloads above.
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM episteme.articles "
            "WHERE source='pubmed' AND source_file='B01.json'"
        )
        assert cur.fetchone()[0] == 1


def test_id_collision_across_source_files_is_replaced(pg_conn, tmp_path, monkeypatch):
    """A full-dump re-release under a NEW source_file (e.g. chembl_35.db ->
    chembl_36.db) reusing a native id from the OLD file must replace the old
    row, not coexist with it -- the normal re-run shape for periodic
    full-dump sources (roadmap SP4 spec section 2)."""
    monkeypatch.setenv("EPISTEME_ACTOR", "t")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    import episteme.config as cfg

    importlib.reload(cfg)
    cfg.get_settings.cache_clear()

    _setup_schema(pg_conn)  # existing helper in this file

    from episteme.data import postgres_loader
    from episteme.data.article_schema import empty_article_row, finalize_row
    from episteme.data.staging_writer import write_rows

    # release A: chembl_35.db carries chembl:CHEMBL25
    row_a = finalize_row(
        {
            **empty_article_row(),
            "id": "chembl:CHEMBL25",
            "source": "chembl",
            "source_file": "chembl_35.db",
            "source_record_id": "CHEMBL25",
            "text": "Compound CHEMBL25 exhibits binding activity old-value.",
            "license": "CC BY-SA",
            "subset": "commercial",
        }
    )
    write_a = write_rows(
        [row_a], tmp_path / "02_processed", source="chembl", source_file="chembl_35.db"
    )
    postgres_loader.load_source_file(
        pg_conn, source="chembl", staging_path=Path(write_a["paths"][0]), run_id="rA"
    )
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.articles WHERE id='chembl:CHEMBL25'")
        assert cur.fetchone()[0] == 1

    # release B: chembl_36.db -- a NEW source_file, but the SAME native id,
    # with different content (the normal shape of a re-release).
    row_b = finalize_row(
        {
            **empty_article_row(),
            "id": "chembl:CHEMBL25",
            "source": "chembl",
            "source_file": "chembl_36.db",
            "source_record_id": "CHEMBL25",
            "text": "Compound CHEMBL25 exhibits binding activity new-value.",
            "license": "CC BY-SA",
            "subset": "commercial",
        }
    )
    write_b = write_rows(
        [row_b], tmp_path / "02_processed", source="chembl", source_file="chembl_36.db"
    )
    postgres_loader.load_source_file(
        pg_conn, source="chembl", staging_path=Path(write_b["paths"][0]), run_id="rB"
    )
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        # exactly ONE row for this id -- the old chembl_35.db row must be gone
        cur.execute("SELECT count(*) FROM episteme.articles WHERE id='chembl:CHEMBL25'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT source_file FROM episteme.articles WHERE id='chembl:CHEMBL25'")
        assert cur.fetchone()[0] == "chembl_36.db"
        cur.execute("SELECT text FROM episteme.article_body WHERE article_id='chembl:CHEMBL25'")
        assert "new-value" in cur.fetchone()[0]
        # no orphaned article_body row from the old release
        cur.execute(
            "SELECT count(*) FROM episteme.article_body ab "
            "JOIN episteme.articles a ON a.id = ab.article_id "
            "WHERE ab.article_id = 'chembl:CHEMBL25'"
        )
        assert cur.fetchone()[0] == 1
