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


# --- streaming loader (batch_size) -------------------------------------------


def _fresh_config(tmp_path, monkeypatch):
    monkeypatch.setenv("EPISTEME_ACTOR", "t")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    import episteme.config as cfg

    importlib.reload(cfg)
    cfg.get_settings.cache_clear()


def _chembl_rows(n_a: int, n_b: int, tag: str) -> list[dict]:
    """Rows alternating between F_a.db and F_b.db (``n_a`` / ``n_b`` of each), so
    every small batch carries both source_files (exercises per-file counters)."""
    from episteme.data.article_schema import empty_article_row, finalize_row

    files: list[str] = []
    a, b = n_a, n_b
    while a or b:
        if a:
            files.append("F_a.db")
            a -= 1
        if b:
            files.append("F_b.db")
            b -= 1
    return [
        finalize_row(
            {
                **empty_article_row(),
                "id": f"chembl:C{i}",
                "source": "chembl",
                "source_file": sf,
                "source_record_id": f"C{i}",
                "year": None if i % 3 == 0 else 2000 + i,
                "authors": [f"A{i}", "B"],
                "retrieved_at": "2026-10-11T00:00:00+00:00",
                "text": f"compound C{i} {tag} " * 3,
                "license": "CC BY-SA",
                "subset": "commercial",
            }
        )
        for i, sf in enumerate(files)
    ]


def _snapshot(conn) -> dict:
    """Order-independent state of every table load_source_file writes."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM episteme.articles ORDER BY source, id, source_file")
        articles = cur.fetchall()
        cur.execute("SELECT * FROM episteme.article_body ORDER BY source, article_id")
        body = cur.fetchall()
        cur.execute(
            "SELECT source, source_file, run_id, input_content_hash, rows_inserted,"
            " rows_deleted FROM episteme._lineage ORDER BY run_id, source_file"
        )
        lineage = cur.fetchall()
        cur.execute(
            "SELECT event_type, object, input_content_hash, rows_affected, run_id"
            " FROM episteme._audit ORDER BY seq"
        )
        audit = cur.fetchall()
    return {"articles": articles, "body": body, "lineage": lineage, "audit": audit}


def _scenario(conn, tmp_path, *, batch_size: int, fmt: str) -> tuple[dict, list[dict]]:
    """Fresh schema; pre-existing rows that every delete path must hit; a load,
    then a shrinking reload of the same shard name. Returns (state, results)."""
    from episteme.data import postgres_loader
    from episteme.data.staging_writer import write_rows

    _setup_schema(conn)
    with conn.cursor() as cur:
        # (d0) id-collision target: an incoming id under an older source_file
        cur.execute(
            "INSERT INTO episteme.articles (id, source, source_file) VALUES (%s, %s, %s)",
            ("chembl:C1", "chembl", "F_old.db"),
        )
        cur.execute(
            "INSERT INTO episteme.article_body (article_id, source, text) VALUES (%s, %s, %s)",
            ("chembl:C1", "chembl", "old body"),
        )
        # a body row with no articles row, for an incoming id: only the id delete hits it
        cur.execute(
            "INSERT INTO episteme.article_body (article_id, source, text) VALUES (%s, %s, %s)",
            ("chembl:C2", "chembl", "orphan body"),
        )
        # another source sharing an id and a source_file name: never touched
        cur.execute(
            "INSERT INTO episteme.articles (id, source, source_file) VALUES (%s, %s, %s)",
            ("chembl:C3", "pubmed", "F_a.db"),
        )
        cur.execute(
            "INSERT INTO episteme.article_body (article_id, source, text) VALUES (%s, %s, %s)",
            ("chembl:C3", "pubmed", "other source"),
        )
    conn.commit()

    root = tmp_path / f"{fmt}-{batch_size}"
    results = []
    for run_id, rows in (("r1", _chembl_rows(5, 4, "v1")), ("r2", _chembl_rows(4, 3, "v2"))):
        info = write_rows(
            rows, root, source="chembl", source_file="F.db", prefer_parquet=fmt == "parquet"
        )
        assert info["format"] == fmt
        res = postgres_loader.load_source_file(
            conn,
            source="chembl",
            staging_path=Path(info["paths"][0]),
            run_id=run_id,
            batch_size=batch_size,
        )
        conn.commit()
        for volatile in ("staging_path", "read_sec", "copy_sec", "rows_per_sec"):
            res.pop(volatile)  # timings differ run to run
        results.append(res)
    return _snapshot(conn), results


def test_multi_batch_load_equals_single_batch(pg_conn, tmp_path, monkeypatch):
    _fresh_config(tmp_path, monkeypatch)

    single, single_res = _scenario(pg_conn, tmp_path, batch_size=50_000, fmt="parquet")
    multi, multi_res = _scenario(pg_conn, tmp_path, batch_size=3, fmt="parquet")

    assert multi == single
    assert multi_res == single_res

    # ... and the shared state is the intended semantics, not merely self-consistent
    r1, r2 = single_res
    assert r1 == {
        "source_file": ["F_a.db", "F_b.db"],
        "rows": 9,
        "deleted": 1,  # (d0): chembl:C1 under F_old.db
        "body_deleted": 2,  # by id: chembl:C1's old body + chembl:C2's orphan body
        "event": "load_replace",
    }
    assert r2 == {
        "source_file": ["F_a.db", "F_b.db"],
        "rows": 7,
        "deleted": 9,  # per source_file: all of r1's rows
        # every chembl body linked through r1's articles rows; the pre-streaming
        # loader also deleted pubmed's chembl:C3 body here (10) -- see (c1)
        "body_deleted": 9,
        "event": "load_replace",
    }
    # (rows_inserted, rows_deleted) per (run_id, source_file)
    assert [r[4:] for r in single["lineage"]] == [(5, 0), (4, 0), (4, 5), (3, 4)]
    assert [(a[0], a[3]) for a in single["audit"]] == [("load_replace", 9), ("load_replace", 7)]
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.articles WHERE source = 'chembl'")
        assert cur.fetchone()[0] == 7
        cur.execute("SELECT count(*) FROM episteme.article_body WHERE source = 'chembl'")
        assert cur.fetchone()[0] == 7
        cur.execute("SELECT count(*) FROM episteme.articles WHERE source = 'pubmed'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM episteme.article_body WHERE source = 'pubmed'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT text FROM episteme.article_body WHERE article_id = 'chembl:C1'")
        assert "v2" in cur.fetchone()[0]


def test_jsonl_shard_streams_like_parquet(pg_conn, tmp_path, monkeypatch):
    _fresh_config(tmp_path, monkeypatch)

    parquet, parquet_res = _scenario(pg_conn, tmp_path, batch_size=50_000, fmt="parquet")
    jsonl, jsonl_res = _scenario(pg_conn, tmp_path, batch_size=3, fmt="jsonl")

    # the file bytes differ by format, so drop the content hash and the shard name
    def _strip(state):
        return {
            **state,
            "lineage": [r[:3] + r[4:] for r in state["lineage"]],
            "audit": [(r[0], r[3], r[4]) for r in state["audit"]],
        }

    assert _strip(jsonl) == _strip(parquet)
    assert jsonl_res == parquet_res


def test_content_hash_is_whole_file_sha256(pg_conn, tmp_path, monkeypatch):
    import hashlib

    _fresh_config(tmp_path, monkeypatch)
    _setup_schema(pg_conn)

    from episteme.data import postgres_loader
    from episteme.data.staging_writer import write_rows

    info = write_rows(_chembl_rows(2, 1, "h"), tmp_path, source="chembl", source_file="F.db")
    shard = Path(info["paths"][0])
    postgres_loader.load_source_file(
        pg_conn, source="chembl", staging_path=shard, run_id="h1", batch_size=2
    )
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT DISTINCT input_content_hash FROM episteme._lineage")
        assert cur.fetchall() == [(hashlib.sha256(shard.read_bytes()).hexdigest(),)]


def test_two_loads_in_one_transaction(pg_conn, tmp_path, monkeypatch):
    """The temp id table must not collide when a caller loads twice before committing."""
    _fresh_config(tmp_path, monkeypatch)
    _setup_schema(pg_conn)

    from episteme.data import postgres_loader
    from episteme.data.staging_writer import write_rows

    a = write_rows(_chembl_rows(2, 0, "a"), tmp_path, source="chembl", source_file="A.db")
    rows_b = [{**r, "id": r["id"] + "b"} for r in _chembl_rows(0, 2, "b")]
    b = write_rows(rows_b, tmp_path, source="chembl", source_file="B.db")
    for info in (a, b):
        postgres_loader.load_source_file(
            pg_conn, source="chembl", staging_path=Path(info["paths"][0]), run_id="t"
        )
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.articles WHERE source = 'chembl'")
        assert cur.fetchone()[0] == 4
