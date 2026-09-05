import importlib
import os

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


# A ~120-word biomedical paragraph, unique, contains no benchmark phrase.
_PARA_A = (
    "Mitochondrial dysfunction has emerged as a central mechanism in the pathogenesis "
    "of numerous neurodegenerative disorders including Parkinson disease and amyotrophic "
    "lateral sclerosis. Impaired oxidative phosphorylation reduces cellular adenosine "
    "triphosphate production and increases the generation of reactive oxygen species "
    "within affected neurons. These reactive species damage lipids proteins and nucleic "
    "acids thereby accelerating the loss of synaptic connections and promoting programmed "
    "cell death. Recent investigations have demonstrated that selective activation of "
    "mitochondrial biogenesis through pharmacological agonists can partially restore "
    "respiratory capacity in cultured dopaminergic cells. Complementary studies in rodent "
    "models indicate that sustained exercise and caloric restriction upregulate the same "
    "transcriptional coactivators improving motor performance and extending survival. "
    "Collectively these findings support the development of therapies that target "
    "mitochondrial quality control pathways in patients with progressive motor decline."
)

# _PARA_A with exactly two interior words changed -> Jaccard on 3-shingles stays
# well above 0.8, so MinHashLSH(threshold=0.8) treats B as a near-duplicate of A.
_PARA_B = _PARA_A.replace("a central mechanism", "a principal mechanism").replace(
    "numerous neurodegenerative disorders", "several neurodegenerative disorders"
)


def test_materialize_dedup_decontam_and_shard(pg_conn, tmp_path, monkeypatch):
    monkeypatch.setenv("EPISTEME_ACTOR", "t")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    import episteme.config as cfg

    importlib.reload(cfg)
    cfg.get_settings.cache_clear()

    _setup_schema(pg_conn)

    from episteme.data import corpus_materializer
    from episteme.data.curate.decontaminate_benchmarks import build_test_ngrams, normalize_text

    # Q: a mock benchmark question with >= 13 words after normalisation. The
    # stock 3-question mock list is all < 13 words (so build_test_ngrams(13,
    # sample_only=True) is empty); this task appends one realistic long entry.
    test_ngrams = build_test_ngrams(13, sample_only=True)
    assert test_ngrams, "mock question list must yield >=1 13-gram (append a long question)"
    Q = (
        "A 55 year old patient with chronic kidney disease presents with severe "
        "hyperkalemia and peaked T waves on the electrocardiogram which medication "
        "should be administered first to stabilize the cardiac membrane"
    )
    assert len(normalize_text(Q)) >= 13

    para_c = (
        "The following clinical vignette is frequently used to assess understanding "
        "of electrolyte emergencies in the acute care setting. " + Q + " Correct "
        "management requires rapid recognition of the underlying rhythm disturbance "
        "and immediate pharmacologic intervention."
    )

    rows = [
        ("art_a", "hash_a", _PARA_A),
        ("art_b", "hash_b", _PARA_B),  # near-dup of art_a -> dropped_dup
        ("art_c", "hash_c", para_c),  # contains Q verbatim -> dropped_contam
    ]

    with pg_conn.cursor() as cur:
        for art_id, content_hash, _text in rows:
            cur.execute(
                "INSERT INTO episteme.articles "
                "(id, source, year, pmid, doi, license, subset, extract_status, content_hash) "
                "VALUES (%s, 'pmc', 2024, %s, %s, 'CC BY', 'commercial', 'ok', %s)",
                (art_id, f"pmid_{art_id}", f"10.1/{art_id}", content_hash),
            )
            cur.execute(
                "INSERT INTO episteme.article_body (article_id, source, year, text) "
                "VALUES (%s, 'pmc', 2024, %s)",
                (art_id, _text),
            )
    pg_conn.commit()  # DuckDB attaches a separate connection -> rows must be visible

    res = corpus_materializer.materialize(
        pg_conn, out_root=tmp_path, run_id="m1", pg_dsn=os.environ["TEST_PG_DSN"]
    )

    assert res["rows_in"] == 3
    assert res["rows_out"] == 1
    assert res["dropped_dup"] >= 1
    assert res["dropped_contam"] >= 1

    shard = tmp_path / "pretrain" / "source=pmc" / "year=2024" / "part-000.parquet"
    assert shard.is_file()
    assert str(shard) in res["shards"]

    import pyarrow.parquet as pq

    table = pq.read_table(shard)
    assert table.num_rows == 1
    assert table.column("id").to_pylist() == ["art_a"]

    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme._audit WHERE event_type='corpus_materialize'")
        assert cur.fetchone()[0] >= 1
