import pytest

from episteme.data import graph_builder as gb

from ._graph_schema import setup_graph_schema

pytestmark = pytest.mark.pg


@pytest.fixture
def conn(pg_conn):
    setup_graph_schema(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.mesh_tree (descriptor_ui, tree_number, source_file) VALUES "
            "('D003920', 'C19.246', 'm'), ('D003924', 'C19.246.300', 'm'), "
            "('D999999', 'C19.2460', 'm')"
        )
        cur.execute(
            "INSERT INTO episteme.article_mesh (pmid, descriptor_ui, descriptor_name, source_file) "
            "VALUES ('1', 'D003920', 'Diabetes Mellitus', 'f'), "
            "('2', 'D003924', 'Diabetes Mellitus, Type 2', 'f'), "
            "('3', 'D999999', 'Unrelated', 'f')"
        )
        cur.execute(
            "INSERT INTO episteme.article_cites (src_pmid, dst_pmid, dst_doi, source_file) "
            "VALUES ('1', '2', NULL, 'f'), ('1', NULL, '10.1/x', 'f'), ('2', NULL, '10.1/y', 'f')"
        )
        cur.execute(
            "INSERT INTO episteme.article_comments (pmid, ref_type, ref_pmid, source_file) "
            "VALUES ('1', 'ErratumIn', '9', 'f'), ('1', 'CommentOn', NULL, 'f')"
        )
        cur.execute(
            "INSERT INTO episteme.article_chemicals (pmid, substance_ui, name, source_file) "
            "VALUES ('1', 'C000001', 'Metformin', 'f')"
        )
        cur.execute(
            "INSERT INTO episteme.article_keywords (pmid, keyword, source_file) "
            "VALUES ('1', 'Insulin', 'f')"
        )
        cur.execute(
            "INSERT INTO episteme.article_authors "
            "(pmid, position, last_name, fore_name, orcid, source_file) "
            "VALUES ('1', 1, 'Smith', 'Jo', '0000-0002-1825-009X', 'f')"
        )
        cur.execute(
            "INSERT INTO episteme.article_facets (pmid, facet, descriptor_ui, rule_version, "
            "source_file) VALUES ('1', 'condition', 'D003920', 1, 'f')"
        )
    return pg_conn


def test_articles_for_mesh(conn):
    assert gb.articles_for_mesh(conn, "D003920") == ["1", "2"]
    assert gb.articles_for_mesh(conn, "D003920", include_descendants=False) == ["1"]
    assert gb.articles_for_mesh(conn, "DNOPE") == []


def test_articles_for_mesh_major_only(conn):
    with conn.cursor() as cur:
        cur.execute("UPDATE episteme.article_mesh SET major_topic = true WHERE pmid = '2'")
    assert gb.articles_for_mesh(conn, "D003920", major_only=True) == ["2"]


def test_cited_by_and_cites_ignore_null_dst(conn):
    # The SQL is already NULL-safe (join / <> drop NULL dst); these rows guard against
    # regressions. The PGQ NULL filter is proven in test_graph_queries_unit.py.
    assert gb.neighbours(conn, "2", kind="cited_by") == ["1"]
    assert gb.neighbours(conn, "1", kind="cited_by") == []
    assert gb.neighbours(conn, "1", kind="cites", hops=3) == ["2"]


def test_other_kinds(conn):
    assert gb.neighbours(conn, "1", kind="comments") == ["CommentOn:", "ErratumIn:9"]
    assert gb.neighbours(conn, "1", kind="comments", ref_type="ErratumIn") == ["ErratumIn:9"]
    assert gb.neighbours(conn, "1", kind="chemical") == ["C000001"]
    assert gb.neighbours(conn, "1", kind="keyword") == ["insulin"]
    assert gb.neighbours(conn, "1", kind="author") == ["1:Jo Smith:0000-0002-1825-009X"]
    assert gb.neighbours(conn, "1", kind="facet") == ["condition:D003920"]


def test_unknown_kind_lists_kinds(conn):
    with pytest.raises(ValueError, match="cited_by"):
        gb.neighbours(conn, "1", kind="bogus")


def test_facet_and_orcid(conn):
    assert gb.articles_for_facet(conn, "condition") == ["1"]
    assert gb.articles_for_facet(conn, "condition", descriptor_ui="D000000") == []
    assert gb.articles_by_orcid(conn, "https://orcid.org/0000-0002-1825-009X") == ["1"]
    assert gb.articles_by_orcid(conn, "garbage") == []
