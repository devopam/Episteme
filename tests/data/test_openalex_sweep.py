"""A pubmed or pmc load removes the openalex rows it duplicates (pg: episteme_test only).

Owner decision 2026-11-12 (spec 2026-11-12-load-followups, section 2). After a
``pubmed`` or ``pmc`` shard is inserted, ``postgres_loader.load_source_file``
deletes every ``openalex`` row (``article_body`` first, then ``articles``) whose
pmid, pmcid or DOI (case-insensitive) matches a row of that shard, and reports
``swept_openalex``. The mirror of the openalex dedup (test_openalex_dedup_load.py),
for the works openalex loaded first.

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


def _row(source: str, id_: str, source_file: str, *, pmid=None, pmcid=None, doi=None) -> dict:
    from episteme.data.article_schema import empty_article_row, finalize_row

    return finalize_row(
        {
            **empty_article_row(),
            "id": id_,
            "source": source,
            "source_file": source_file,
            "source_record_id": id_.split(":", 1)[1],
            "pmid": pmid,
            "pmcid": pmcid,
            "doi": doi,
            "title": f"title {id_}",
            "abstract": f"an abstract about acetylcholinesterase {id_} " * 5,
            "year": 2020,
            "license": "CC0" if source == "openalex" else "CC BY",
            "subset": "commercial",
        }
    )


def _oa(native: str, *, pmid=None, pmcid=None, doi=None) -> dict:
    return _row("openalex", f"openalex:{native}", "oa_part", pmid=pmid, pmcid=pmcid, doi=doi)


def _stage(tmp_path: Path, source: str, source_file: str, rows: list[dict]) -> Path:
    from episteme.data.article_schema import DEDUP_IDS_META_KEY, DEDUP_IDS_VERSION
    from episteme.data.staging_writer import write_rows

    metadata = {DEDUP_IDS_META_KEY: DEDUP_IDS_VERSION} if source == "openalex" else None
    info = write_rows(
        rows,
        tmp_path / "02_processed",
        source=source,
        source_file=source_file,
        metadata=metadata,
    )
    return Path(info["paths"][0])


def _load_openalex(loader, conn, tmp_path, rows: list[dict]) -> None:
    """Load the openalex rows first, with no pubmed / pmc row in the database,
    so the openalex dedup keeps every one of them."""
    shard = _stage(tmp_path, "openalex", "oa_part", rows)
    r = loader.load_source_file(conn, source="openalex", staging_path=shard, run_id="oa")
    conn.commit()
    assert r["loaded"] == len(rows)


def _pubmed_shard(tmp_path, *rows: dict, source_file: str = "pubmed26n0001.xml.gz") -> Path:
    return _stage(tmp_path, "pubmed", source_file, list(rows))


def _pubmed(pmid: str, *, doi=None, pmcid=None, source_file="pubmed26n0001.xml.gz") -> dict:
    return _row("pubmed", f"pmid:{pmid}", source_file, pmid=pmid, pmcid=pmcid, doi=doi)


def _openalex(conn) -> dict[str, list[str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM episteme.articles_openalex ORDER BY id")
        hot = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT article_id FROM episteme.article_body_openalex ORDER BY article_id")
        body = [r[0] for r in cur.fetchall()]
    return {"hot": hot, "body": body}


def _lineage_and_audit(conn, run_id: str) -> tuple[list, object]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT dedup_counts FROM episteme._lineage WHERE run_id = %s ORDER BY source_file",
            (run_id,),
        )
        lineage = [r[0] for r in cur.fetchall()]
        cur.execute(
            "SELECT new_value FROM episteme._audit "
            "WHERE run_id = %s AND event_type IN ('load_commit', 'load_replace')",
            (run_id,),
        )
        new_value = cur.fetchone()[0]
    if isinstance(new_value, str):
        new_value = json.loads(new_value)
    return lineage, new_value


def test_pubmed_load_sweeps_matching_openalex_rows(loader, pg_conn, tmp_path):
    # W1 matches the pubmed row by pmid AND by DOI: one row, counted once.
    _load_openalex(
        loader,
        pg_conn,
        tmp_path,
        [_oa("W1", pmid="100", doi="10.1000/x1"), _oa("W2", pmid="999", doi="10.9999/new")],
    )
    shard = _pubmed_shard(tmp_path, _pubmed("100", doi="10.1000/X1"))

    r = loader.load_source_file(pg_conn, source="pubmed", staging_path=shard, run_id="p1")
    pg_conn.commit()

    assert r["swept_openalex"] == 1
    assert r["rows"] == 1
    assert r["deleted"] == 0  # the sweep is not this shard's replaced rows
    assert r["event"] == "load_commit"
    assert _openalex(pg_conn) == {"hot": ["openalex:W2"], "body": ["openalex:W2"]}
    lineage, new_value = _lineage_and_audit(pg_conn, "p1")
    assert lineage == [{"swept_openalex": 1}]
    assert new_value == {"swept_openalex": 1}
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.articles WHERE source = 'pubmed'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM episteme.article_body WHERE source = 'pubmed'")
        assert cur.fetchone()[0] == 1


def test_sweep_matches_doi_case_insensitively(loader, pg_conn, tmp_path):
    _load_openalex(loader, pg_conn, tmp_path, [_oa("W3", doi="10.1000/abc"), _oa("W9")])
    shard = _pubmed_shard(tmp_path, _pubmed("200", doi="10.1000/ABC"))

    r = loader.load_source_file(pg_conn, source="pubmed", staging_path=shard, run_id="p1")
    pg_conn.commit()

    assert r["swept_openalex"] == 1
    assert _openalex(pg_conn) == {"hot": ["openalex:W9"], "body": ["openalex:W9"]}


def test_sweep_removes_every_match(loader, pg_conn, tmp_path):
    """Two openalex works with one DOI: the probe has no LIMIT, so both go."""
    _load_openalex(
        loader,
        pg_conn,
        tmp_path,
        [_oa("W4", doi="10.2000/dup"), _oa("W5", doi="10.2000/dup"), _oa("W9")],
    )
    shard = _pubmed_shard(tmp_path, _pubmed("300", doi="10.2000/DUP"))

    r = loader.load_source_file(pg_conn, source="pubmed", staging_path=shard, run_id="p1")
    pg_conn.commit()

    assert r["swept_openalex"] == 2
    assert _openalex(pg_conn) == {"hot": ["openalex:W9"], "body": ["openalex:W9"]}
    lineage, new_value = _lineage_and_audit(pg_conn, "p1")
    assert lineage == [{"swept_openalex": 2}]
    assert new_value == {"swept_openalex": 2}


def test_pmc_load_sweeps_too(loader, pg_conn, tmp_path):
    _load_openalex(loader, pg_conn, tmp_path, [_oa("W6", pmcid="PMC300"), _oa("W9")])
    row = _row("pmc", "pmcid:PMC300", "B01.json", pmcid="PMC300")
    shard = _stage(tmp_path, "pmc", "B01.json", [row])

    r = loader.load_source_file(pg_conn, source="pmc", staging_path=shard, run_id="c1")
    pg_conn.commit()

    assert r["swept_openalex"] == 1
    assert _openalex(pg_conn) == {"hot": ["openalex:W9"], "body": ["openalex:W9"]}
    lineage, new_value = _lineage_and_audit(pg_conn, "c1")
    assert lineage == [{"swept_openalex": 1}]
    assert new_value == {"swept_openalex": 1}


def test_sweep_on_reload_is_zero(loader, pg_conn, tmp_path):
    """A re-load of the same shard (restart / --force) finds nothing left to sweep."""
    _load_openalex(loader, pg_conn, tmp_path, [_oa("W1", pmid="100"), _oa("W9")])
    shard = _pubmed_shard(tmp_path, _pubmed("100"))
    r1 = loader.load_source_file(pg_conn, source="pubmed", staging_path=shard, run_id="p1")
    pg_conn.commit()
    assert r1["swept_openalex"] == 1

    r2 = loader.load_source_file(pg_conn, source="pubmed", staging_path=shard, run_id="p2")
    pg_conn.commit()

    assert r2["swept_openalex"] == 0
    assert r2["event"] == "load_replace"
    assert r2["deleted"] == 1
    assert _openalex(pg_conn) == {"hot": ["openalex:W9"], "body": ["openalex:W9"]}
    lineage, new_value = _lineage_and_audit(pg_conn, "p2")
    assert lineage == [{"swept_openalex": 0}]
    assert new_value == {"swept_openalex": 0}


def test_no_match_reports_zero(loader, pg_conn, tmp_path):
    _load_openalex(
        loader, pg_conn, tmp_path, [_oa("W1", pmid="100", pmcid="PMC1", doi="10.1000/a")]
    )
    shard = _pubmed_shard(tmp_path, _pubmed("555", doi="10.5555/OTHER", pmcid="PMC555"))

    r = loader.load_source_file(pg_conn, source="pubmed", staging_path=shard, run_id="p1")
    pg_conn.commit()

    assert r["swept_openalex"] == 0
    assert _openalex(pg_conn) == {"hot": ["openalex:W1"], "body": ["openalex:W1"]}
    lineage, new_value = _lineage_and_audit(pg_conn, "p1")
    assert lineage == [{"swept_openalex": 0}]
    assert new_value == {"swept_openalex": 0}


def test_non_sweeping_source_has_no_sweep_key(loader, pg_conn, tmp_path):
    """chembl neither dedups nor sweeps: its pmid never removes an openalex row."""
    _load_openalex(loader, pg_conn, tmp_path, [_oa("W1", pmid="100")])
    row = _row("chembl", "chembl:CHEMBL25", "chembl_36.db", pmid="100")
    shard = _stage(tmp_path, "chembl", "chembl_36.db", [row])

    r = loader.load_source_file(pg_conn, source="chembl", staging_path=shard, run_id="m1")
    pg_conn.commit()

    assert "swept_openalex" not in r
    assert _openalex(pg_conn) == {"hot": ["openalex:W1"], "body": ["openalex:W1"]}
    lineage, new_value = _lineage_and_audit(pg_conn, "m1")
    assert lineage == [None]
    assert new_value is None


def test_sweep_plan_uses_openalex_indexes(loader, pg_conn, tmp_path):
    """Each sweep probe walks the key table and reads articles_openalex only by an
    index probe on that key's index: no Seq Scan, no Bitmap scan, no Sort /
    Unique / Hash (the turned-around join of PR 110)."""
    key_index = {"pmid": "_pmid_idx", "pmcid": "_pmcid_idx", "doi": "_doi_idx"}
    _load_openalex(
        loader,
        pg_conn,
        tmp_path,
        [_oa("W1", pmid="100", pmcid="PMC100", doi="10.1000/a"), _oa("W9")],
    )
    sf = "pubmed26n0001.xml.gz"
    shard = _pubmed_shard(tmp_path, _pubmed("100", pmcid="PMC100", doi="10.1000/A"))
    loader.load_source_file(pg_conn, source="pubmed", staging_path=shard, run_id="p1")
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        loader._create_sweep_rows(cur, [sf], "pubmed")
        for key in loader.DEDUP_KEYS:
            loader._create_sweep_keys(cur, key)
            with loader._forced_nested_loops(cur), loader._no_bitmap_scans(cur):
                cur.execute("EXPLAIN " + loader._sweep_probe_sql(key, "openalex"))
                plan = "\n".join(r[0] for r in cur.fetchall())
            assert "_sweep_keys" in plan, plan
            assert "articles_openalex" in plan, plan
            for bad in ("Sort", "Unique", "Bitmap", "Hash"):
                assert bad not in plan, plan
            for line in plan.splitlines():
                if " on articles_openalex" in line:
                    assert "Seq Scan" not in line, plan
                    assert "Index" in line and key_index[key] in line, plan
            assert "articles_pubmed" not in plan and "articles_default" not in plan, plan
    pg_conn.rollback()


def test_sweep_refuses_when_indexes_missing_and_openalex_rows_exist(loader, pg_conn, tmp_path):
    """Ruling R2: a --bulk drop removes the articles_openalex key indexes. With
    openalex rows present, the sweep would read the whole partition per shard:
    the load refuses the shard, before any work, and names the rebuild."""
    _load_openalex(loader, pg_conn, tmp_path, [_oa("W1", pmid="100")])
    shard = _pubmed_shard(tmp_path, _pubmed("100"))
    with pg_conn.cursor() as cur:
        # Not committed: the loader uses this connection, and the rollback below
        # restores the index, so episteme_test keeps a complete schema.
        cur.execute("DROP INDEX episteme.articles_pmid_idx")
    with pytest.raises(RuntimeError, match="articles_openalex_pmid_idx") as exc:
        loader.load_source_file(pg_conn, source="pubmed", staging_path=shard, run_id="x1")
    assert "bulk_load_indexes.sh rebuild" in str(exc.value)
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.articles WHERE source = 'pubmed'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT to_regclass('episteme.articles_pmid_idx') IS NOT NULL")
        assert cur.fetchone()[0]
    assert _openalex(pg_conn) == {"hot": ["openalex:W1"], "body": ["openalex:W1"]}


def test_sweep_skips_when_indexes_missing_and_no_openalex_rows(loader, pg_conn, tmp_path):
    """Ruling R2: with no openalex row, a --bulk pubmed load goes on; the sweep
    is skipped and reports 0."""
    shard = _pubmed_shard(tmp_path, _pubmed("100"))
    with pg_conn.cursor() as cur:
        cur.execute("DROP INDEX episteme.articles_pmid_idx")  # rolled back below
    r = loader.load_source_file(pg_conn, source="pubmed", staging_path=shard, run_id="b1")
    assert r["swept_openalex"] == 0
    assert r["rows"] == 1
    lineage, new_value = _lineage_and_audit(pg_conn, "b1")
    assert lineage == [{"swept_openalex": 0}]
    assert new_value == {"swept_openalex": 0}
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('episteme.articles_pmid_idx') IS NOT NULL")
        assert cur.fetchone()[0]
