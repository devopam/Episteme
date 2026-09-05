import importlib

import pytest

pytestmark = pytest.mark.pg

_MIN_JATS = """<article>
  <front><article-meta>
    <kwd-group kwd-group-type="MeSH">
      <kwd>Acetylcholinesterase</kwd>
      <kwd>Neurodegeneration</kwd>
    </kwd-group>
  </article-meta></front>
  <back><ref-list><ref><element-citation>
    <pub-id pub-id-type="pmid">222</pub-id>
  </element-citation></ref></ref-list></back>
</article>
"""


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


def _seed(pg_conn, tmp_path, monkeypatch):
    monkeypatch.setenv("EPISTEME_ACTOR", "t")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    import episteme.config as cfg

    importlib.reload(cfg)
    cfg.get_settings.cache_clear()

    _setup_schema(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO episteme.articles (id, source, source_file, pmid, pmcid, year)
            VALUES ('pmcid:PMC1', 'pmc', 'B01.xml', '111', 'PMC1', 2024)
            """
        )
    pg_conn.commit()

    raw = tmp_path / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "PMC1.1.xml").write_text(_MIN_JATS, encoding="utf-8")
    return raw


def test_build_and_neighbours(pg_conn, tmp_path, monkeypatch):
    raw = _seed(pg_conn, tmp_path, monkeypatch)

    # a pmid-less article must be counted+skipped, not silently excluded.
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO episteme.articles (id, source, source_file, pmid, pmcid, year)
            VALUES ('pmcid:PMC2', 'pmc', 'B01.xml', NULL, 'PMC2', 2024)
            """
        )
    pg_conn.commit()

    from episteme.data import graph_builder

    res = graph_builder.build(pg_conn, source="pmc", raw_dir=raw, run_id="g1")
    pg_conn.commit()

    assert res["mesh"] == 2
    assert res["cites"] == 1
    assert res["missing_xml"] == 0
    assert res["skipped_no_pmid"] == 1

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.article_mesh WHERE pmid='111'")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT descriptor_ui FROM episteme.article_mesh WHERE pmid='111' LIMIT 1")
        assert cur.fetchone()[0] is None
        cur.execute(
            "SELECT count(*) FROM episteme.article_cites WHERE src_pmid='111' AND dst_pmid='222'"
        )
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM episteme._audit WHERE event_type='graph_commit'")
        assert cur.fetchone()[0] >= 1

    assert graph_builder.neighbours(pg_conn, "111", kind="mesh") == [
        "Acetylcholinesterase",
        "Neurodegeneration",
    ]
    assert graph_builder.neighbours(pg_conn, "111", kind="cites") == ["222"]
    assert graph_builder._neighbours_cte(pg_conn, "111", 1, "cites") == ["222"]

    with pytest.raises(ValueError):
        graph_builder.neighbours(pg_conn, "111", kind="bogus")

    # idempotency: a second build over the same source_file must not duplicate.
    res2 = graph_builder.build(pg_conn, source="pmc", raw_dir=raw, run_id="g2")
    pg_conn.commit()
    assert res2["mesh"] == 2
    assert res2["cites"] == 1
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.article_mesh WHERE pmid='111'")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT count(*) FROM episteme.article_cites WHERE src_pmid='111'")
        assert cur.fetchone()[0] == 1

    with pytest.raises(NotImplementedError):
        graph_builder.build(pg_conn, source="pubmed", raw_dir=raw, run_id="g3")


def test_pgq_path_when_available(pg_conn, tmp_path, monkeypatch):
    from episteme.data import graph_builder

    if not graph_builder._pgq_available(pg_conn):
        pytest.skip("SQL/PGQ episteme_graph absent on this build")

    raw = _seed(pg_conn, tmp_path, monkeypatch)
    graph_builder.build(pg_conn, source="pmc", raw_dir=raw, run_id="g1")
    pg_conn.commit()
    assert set(graph_builder._neighbours_pgq(pg_conn, "111", 1)) == {"222"}
