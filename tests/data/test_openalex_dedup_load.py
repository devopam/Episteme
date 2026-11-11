"""OpenAlex load skips works that pubmed or pmc already hold (pg: episteme_test only).

Owner decision 2026-11-11. ``postgres_loader.load_source_file`` for ``openalex``
COPYs the shard into temp tables, removes the rows whose pmid, else pmcid, else
DOI (case-insensitive) matches a ``pubmed`` or ``pmc`` row, and inserts the rest
into ``articles_openalex`` / ``article_body_openalex`` (migration 0012).

The schema fixture drops and rebuilds ``episteme`` in ``episteme_test``
(schema.sql plus migrations 0002-0012): re-apply the migrations there before
any ``--sample load``.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from ._graph_schema import MIG, REPO

pytestmark = pytest.mark.pg


def _schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
        cur.execute((REPO / "src/episteme/data/db/extensions.sql").read_text(encoding="utf-8"))
        cur.execute((REPO / "src/episteme/data/db/schema.sql").read_text(encoding="utf-8"))
        # 0001 needs pgvector (not on the local build); every other migration applies.
        for name in sorted(p.name for p in MIG.glob("00*.sql")):
            if not name.startswith("0001_"):
                cur.execute((MIG / name).read_text(encoding="utf-8"))
    conn.commit()


@pytest.fixture
def loader(pg_conn, tmp_path, monkeypatch):
    monkeypatch.setenv("EPISTEME_ACTOR", "t")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    import episteme.config as cfg

    importlib.reload(cfg)
    cfg.get_settings.cache_clear()
    _schema(pg_conn)
    from episteme.data import postgres_loader

    return postgres_loader


def _seed(conn) -> None:
    """pubmed and pmc rows the openalex shard matches, plus a chembl row whose
    pmid must NOT cause a skip (only pubmed and pmc count)."""
    rows = [
        ("pubmed:100", "pubmed", "100", None, "10.1000/Mixed.CASE.1"),
        ("pubmed:101", "pubmed", "101", None, "10.1016/S0140-6736(20)30183-5"),
        ("pmcid:PMC200", "pmc", None, "PMC200", None),
        ("chembl:1", "chembl", "300", None, None),
    ]
    with conn.cursor() as cur:
        for id_, source, pmid, pmcid, doi in rows:
            cur.execute(
                "INSERT INTO episteme.articles (id, source, source_file, year, pmid, pmcid, doi) "
                "VALUES (%s, %s, 'seed', 2020, %s, %s, %s)",
                (id_, source, pmid, pmcid, doi),
            )
        cur.execute("ANALYZE episteme.articles")
    conn.commit()


def _oa_row(native: str, *, pmid=None, pmcid=None, doi=None) -> dict:
    from episteme.data.article_schema import empty_article_row, finalize_row

    return finalize_row(
        {
            **empty_article_row(),
            "id": f"openalex:{native}",
            "source": "openalex",
            "source_file": "oa_part.parquet",
            "source_record_id": native,
            "pmid": pmid,
            "pmcid": pmcid,
            "doi": doi,
            "title": f"title {native}",
            "abstract": f"an abstract about acetylcholinesterase {native} " * 5,
            "year": 2020,
            "license": "CC0",
            "subset": "commercial",
        }
    )


def _shard(tmp_path: Path) -> Path:
    from episteme.data.staging_writer import write_rows

    rows = [
        # pmid matches pubmed:100; its DOI matches too: counted once, as pmid
        _oa_row("W1", pmid="100", doi="10.1000/mixed.case.1"),
        # pmcid matches the pmc row; no pmid
        _oa_row("W2", pmcid="PMC200"),
        # DOI only, lower case vs pubmed's mixed case
        _oa_row("W3", doi="10.1016/s0140-6736(20)30183-5"),
        # new work: ids that match nothing
        _oa_row("W4", pmid="999", pmcid="PMC999", doi="10.9999/new"),
        # pmid of a chembl row: not pubmed or pmc, so it loads
        _oa_row("W5", pmid="300"),
    ]
    info = write_rows(rows, tmp_path / "02_processed", source="openalex", source_file="oa_part")
    return Path(info["paths"][0])


def _loaded(conn) -> dict[str, list[str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM episteme.articles_openalex ORDER BY id")
        hot = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT article_id FROM episteme.article_body_openalex ORDER BY article_id")
        body = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT count(*) FROM episteme.articles_default WHERE source = 'openalex'")
        in_default = cur.fetchone()[0]
    return {"hot": hot, "body": body, "in_default": in_default}


EXPECTED_COUNTS = {
    "rows_in": 5,
    "skipped_pmid": 1,
    "skipped_pmcid": 1,
    "skipped_doi": 1,
    "loaded": 2,
}


def test_openalex_load_skips_pubmed_and_pmc_works(loader, pg_conn, tmp_path):
    _seed(pg_conn)
    shard = _shard(tmp_path)

    r1 = loader.load_source_file(pg_conn, source="openalex", staging_path=shard, run_id="r1")
    pg_conn.commit()

    assert {k: r1[k] for k in EXPECTED_COUNTS} == EXPECTED_COUNTS
    assert r1["rows"] == 2
    assert r1["event"] == "load_commit"
    assert _loaded(pg_conn) == {
        "hot": ["openalex:W4", "openalex:W5"],
        "body": ["openalex:W4", "openalex:W5"],
        "in_default": 0,
    }
    with pg_conn.cursor() as cur:
        cur.execute("SELECT pmid, pmcid, doi FROM episteme.articles WHERE id = 'openalex:W4'")
        assert cur.fetchone() == ("999", "PMC999", "10.9999/new")
        cur.execute(
            "SELECT rows_inserted FROM episteme._lineage "
            "WHERE source = 'openalex' AND run_id = 'r1'"
        )
        assert [r[0] for r in cur.fetchall()] == [2]
        cur.execute(
            "SELECT rows_affected, new_value FROM episteme._audit "
            "WHERE event_type = 'load_commit' AND run_id = 'r1'"
        )
        rows_affected, new_value = cur.fetchone()
        if isinstance(new_value, str):
            new_value = json.loads(new_value)
        assert rows_affected == 2
        assert new_value == EXPECTED_COUNTS
        # the seeded rows are untouched
        cur.execute("SELECT count(*) FROM episteme.articles WHERE source <> 'openalex'")
        assert cur.fetchone()[0] == 4

    # Re-load of the same shard (restart / --force): same counts, no duplicates.
    r2 = loader.load_source_file(pg_conn, source="openalex", staging_path=shard, run_id="r2")
    pg_conn.commit()
    assert {k: r2[k] for k in EXPECTED_COUNTS} == EXPECTED_COUNTS
    assert r2["event"] == "load_replace"
    assert r2["deleted"] == 2
    assert _loaded(pg_conn)["hot"] == ["openalex:W4", "openalex:W5"]
    assert _loaded(pg_conn)["body"] == ["openalex:W4", "openalex:W5"]


def test_other_sources_report_no_dedup_counts(loader, pg_conn, tmp_path):
    """The dedup path is openalex only: a pmc load returns the old stats shape."""
    from episteme.data.article_schema import empty_article_row, finalize_row
    from episteme.data.staging_writer import write_rows

    _seed(pg_conn)
    row = finalize_row(
        {
            **empty_article_row(),
            "id": "pmcid:PMC1",
            "source": "pmc",
            "source_file": "B01.json",
            "pmcid": "PMC1",
            "pmid": "100",  # the same pmid as pubmed:100: pmc never dedups
            "year": 2024,
            "abstract": "an abstract about acetylcholinesterase " * 5,
            "license": "CC BY",
            "subset": "commercial",
        }
    )
    info = write_rows([row], tmp_path / "02_processed", source="pmc", source_file="B01.json")
    r = loader.load_source_file(
        pg_conn, source="pmc", staging_path=Path(info["paths"][0]), run_id="p1"
    )
    pg_conn.commit()
    assert r["rows"] == 1
    assert "skipped_pmid" not in r
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT new_value FROM episteme._audit "
            "WHERE run_id = 'p1' AND event_type = 'load_commit'"
        )
        assert cur.fetchone()[0] is None


def test_dedup_plan_probes_indexes_not_seq_scans(loader, pg_conn):
    """Each dedup probe reads the pubmed / pmc partitions by index (no Seq Scan),
    even on empty, unanalyzed partitions."""
    with pg_conn.cursor() as cur:
        loader._create_dedup_tables(cur)
        with loader._forced_nested_loops(cur):
            for key in loader.DEDUP_KEYS:
                cur.execute("EXPLAIN " + loader._dedup_delete_sql(key, ("pubmed", "pmc")))
                plan = "\n".join(r[0] for r in cur.fetchall())
                assert "articles_pubmed" in plan and "articles_pmc" in plan, plan
                for line in plan.splitlines():
                    if "Seq Scan" in line:
                        assert "articles_pubmed" not in line and "articles_pmc" not in line, plan
                assert "articles_default" not in plan and "articles_chembl" not in plan, plan
    pg_conn.rollback()


def test_openalex_load_refuses_without_the_dedup_indexes(loader, pg_conn, tmp_path):
    """A --bulk drop (or a missing 0012) must fail the shard, not fall back to a
    sequential scan of pubmed per shard."""
    shard = _shard(tmp_path)
    with pg_conn.cursor() as cur:
        cur.execute("DROP INDEX episteme.articles_pmid_idx")
    pg_conn.commit()
    with pytest.raises(RuntimeError, match="articles_pmid_idx"):
        loader.load_source_file(pg_conn, source="openalex", staging_path=shard, run_id="x1")
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.articles WHERE source = 'openalex'")
        assert cur.fetchone()[0] == 0
