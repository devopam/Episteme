import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.pg

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_0002 = REPO_ROOT / "src/episteme/data/db/migrations/0002_container_and_book_parts.sql"
MIGRATION_0003 = REPO_ROOT / "src/episteme/data/db/migrations/0003_mesh_hierarchy.sql"

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


def _configure(tmp_path, monkeypatch):
    monkeypatch.setenv("EPISTEME_ACTOR", "t")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    import episteme.config as cfg

    importlib.reload(cfg)
    cfg.get_settings.cache_clear()


def _seed(pg_conn, tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)

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
        graph_builder.build(pg_conn, source="chembl", raw_dir=raw, run_id="g3")


def test_build_populates_article_parts(pg_conn, tmp_path, monkeypatch):
    # schema.sql carries container_id/book_meta but NOT episteme.article_parts
    # (Task 2 put that DDL in migration 0002 only); apply 0002 after the schema
    # rebuild. It is idempotent -- same pattern as tests/test_migrations.py.
    _configure(tmp_path, monkeypatch)
    _setup_schema(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(MIGRATION_0002.read_text(encoding="utf-8"))
    pg_conn.commit()

    # one book row (container_id NULL) + two part rows pointing at it. year=0 is
    # mandatory: articles_bookshelf has a y0 range partition but no DEFAULT.
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO episteme.articles (id, source, source_file, container_id, book_meta, year)
            VALUES
              ('bookshelf:NBK1',    'bookshelf', 'NBK1.tar.gz', NULL,             '{}'::jsonb, 0),
              ('bookshelf:NBK1:p1', 'bookshelf', 'NBK1.tar.gz', 'bookshelf:NBK1', NULL,        0),
              ('bookshelf:NBK1:p2', 'bookshelf', 'NBK1.tar.gz', 'bookshelf:NBK1', NULL,        0)
            """
        )
    pg_conn.commit()

    from episteme.data import graph_builder

    res = graph_builder.build(pg_conn, source="bookshelf", raw_dir=tmp_path, run_id="p1")
    pg_conn.commit()

    assert res["parts"] == 2
    assert "cites" in res and res["cites"] == 0
    assert res["mesh"] == 0
    assert res["missing_xml"] == 0
    assert res["skipped_no_pmid"] == 0
    assert res["source_files"] == []

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.article_parts")
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT count(*) FROM episteme._audit WHERE event_type='graph_commit'")
        assert cur.fetchone()[0] >= 1

    assert set(graph_builder.neighbours(pg_conn, "bookshelf:NBK1", kind="part")) == {
        "bookshelf:NBK1:p1",
        "bookshelf:NBK1:p2",
    }
    # reverse edge, via the UNION in the part CTE.
    assert graph_builder.neighbours(pg_conn, "bookshelf:NBK1:p1", kind="part") == ["bookshelf:NBK1"]

    # idempotency: a second build over the same source_file must not duplicate.
    res2 = graph_builder.build(pg_conn, source="bookshelf", raw_dir=tmp_path, run_id="p2")
    pg_conn.commit()
    assert res2["parts"] == 2
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.article_parts")
        assert cur.fetchone()[0] == 2


def test_build_populates_mesh_hierarchy(pg_conn, tmp_path, monkeypatch):
    _setup_schema(pg_conn)  # existing helper
    with pg_conn.cursor() as cur:
        cur.execute(MIGRATION_0002.read_text(encoding="utf-8"))
        cur.execute(MIGRATION_0003.read_text(encoding="utf-8"))
    pg_conn.commit()

    # a tiny real-shaped MeSH descriptor XML: D003920 (child) under D003924's
    # tree number prefix (parent), per NLM's DescriptorRecordSet shape.
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "desc2026.xml").write_text(
        "<DescriptorRecordSet>"
        "<DescriptorRecord><DescriptorUI>D003924</DescriptorUI>"
        "<DescriptorName><String>Diabetes Mellitus</String></DescriptorName>"
        "<TreeNumberList><TreeNumber>C18.452.394.750</TreeNumber></TreeNumberList>"
        "</DescriptorRecord>"
        "<DescriptorRecord><DescriptorUI>D003920</DescriptorUI>"
        "<DescriptorName><String>Diabetes Mellitus, Type 2</String></DescriptorName>"
        "<TreeNumberList><TreeNumber>C18.452.394.750.149</TreeNumber></TreeNumberList>"
        "</DescriptorRecord>"
        "</DescriptorRecordSet>",
        encoding="utf-8",
    )

    # seed the loaded articles rows graph_builder reads to find source_files
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.articles (id, source, source_file, source_record_id) "
            "VALUES (%s, 'mesh', %s, %s), (%s, 'mesh', %s, %s)",
            ("mesh:D003924", "desc2026.xml", "D003924", "mesh:D003920", "desc2026.xml", "D003920"),
        )
    pg_conn.commit()

    from episteme.data.graph_builder import build, neighbours

    res = build(pg_conn, source="mesh", raw_dir=raw, run_id="t")
    pg_conn.commit()
    assert res["mesh_hierarchy"] == 1
    assert neighbours(pg_conn, "D003924", kind="mesh_child") == ["D003920"]
    assert neighbours(pg_conn, "D003920", kind="mesh_parent") == ["D003924"]

    # idempotency: a second build over the same source_file must not duplicate.
    res2 = build(pg_conn, source="mesh", raw_dir=raw, run_id="t2")
    pg_conn.commit()
    assert res2["mesh_hierarchy"] == 1
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episteme.mesh_hierarchy")
        assert cur.fetchone()[0] == 1


def test_pgq_path_when_available(pg_conn, tmp_path, monkeypatch):
    from episteme.data import graph_builder

    if not graph_builder._pgq_available(pg_conn):
        pytest.skip("SQL/PGQ episteme_graph absent on this build")

    raw = _seed(pg_conn, tmp_path, monkeypatch)
    graph_builder.build(pg_conn, source="pmc", raw_dir=raw, run_id="g1")
    pg_conn.commit()
    assert set(graph_builder._neighbours_pgq(pg_conn, "111", 1)) == {"222"}
