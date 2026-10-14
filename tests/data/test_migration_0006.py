import pytest

from ._graph_schema import MIG, setup_graph_schema

pytestmark = pytest.mark.pg


def test_0006_tables_indexes_rules_and_rights(pg_conn):
    setup_graph_schema(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute((MIG / "0006_pubmed_graph.sql").read_text(encoding="utf-8"))  # twice
        cur.execute(
            "SELECT count(*) FROM pg_inherits "
            "WHERE inhparent = 'episteme.article_authors'::regclass"
        )
        assert cur.fetchone()[0] == 8
        cur.execute("SELECT count(*) FROM episteme.mesh_facet_rule WHERE rule_version = 1")
        assert cur.fetchone()[0] == 31
        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname='episteme' "
            "AND indexname LIKE '%%_source_file_idx' AND tablename IN ("
            "'article_mesh','article_cites','article_comments','article_chemicals',"
            "'article_keywords','article_authors','article_facets')"
        )
        assert len(cur.fetchall()) == 7
        cur.execute(
            "SELECT has_table_privilege('episteme_app','episteme.article_authors','INSERT'), "
            "has_table_privilege('episteme_app','episteme._graph_lineage','INSERT'), "
            "has_table_privilege('episteme_app','episteme.mesh_facet_rule','SELECT'), "
            "has_table_privilege('episteme_app','episteme.mesh_facet_rule','INSERT')"
        )
        assert cur.fetchone() == (True, True, True, False)
        cur.execute(
            "INSERT INTO episteme.article_cites (src_pmid, dst_pmid, ref_text) "
            "VALUES ('1', NULL, 'x')"
        )
    pg_conn.rollback()
