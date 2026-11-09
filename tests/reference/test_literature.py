"""literature against fixture MeSH rows (pg)."""

from __future__ import annotations

import pytest

from episteme.reference.literature import literature

from ._pbrer_db import KW

pytestmark = pytest.mark.pg


@pytest.fixture
def mesh(ref_db):
    with ref_db.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.article_mesh "
            "(pmid, descriptor_ui, descriptor_name, major_topic, qualifiers, source_file) VALUES "
            "('100', 'D008687', 'Metformin', true, ARRAY['therapeutic use'], 'pubmed/a.xml'), "
            "('200', 'D008687', 'Metformin', true, ARRAY['Adverse Effects'], 'pubmed/b.xml'), "
            "('200', 'D056486', 'Chemical and Drug Induced Liver Injury', false, NULL, "
            " 'pubmed/b.xml'), "
            "('300', 'D001241', 'Aspirin', true, NULL, 'pubmed/c.xml'), "
            "('400', 'D008687', 'Metformin', false, NULL, 'pubmed/d.xml')"
        )
        cur.execute(
            "INSERT INTO episteme.articles (id, source, pmid, year) VALUES "
            "('pubmed:100', 'pubmed', '100', 2020), ('pubmed:200', 'pubmed', '200', 2023), "
            "('pmc:PMC200', 'pmc', '200', 2023), ('pubmed:300', 'pubmed', '300', 2024)"
        )
    ref_db.commit()
    return ref_db


def test_literature_cites_pmids(mesh):
    res = literature(mesh, 2, None, **KW)  # the salt reaches the moiety's names
    # Newest first; PMID 400 has no year and comes last; no duplicate from PMC.
    assert [c.provenance.record_key for c in res.claims] == ["pmid:200", "pmid:100", "pmid:400"]
    c = res.claims[0]
    assert c.value == "200"
    assert c.method == "fetched"
    assert c.provenance.source == "pubmed"
    assert c.provenance.source_file == "pubmed/b.xml"
    assert "2023" in c.text and "Metformin" in c.text
    assert res.coverage.matched == 3
    assert res.coverage.missing == ()


def test_literature_topic_matches_descriptor_or_qualifier(mesh):
    by_qualifier = literature(mesh, 1, "adverse effects", **KW)
    assert [c.value for c in by_qualifier.claims] == ["200"]
    by_descriptor = literature(mesh, 1, "Chemical and Drug Induced Liver Injury", **KW)
    assert [c.value for c in by_descriptor.claims] == ["200"]


def test_literature_limit_is_noted(mesh):
    res = literature(mesh, 1, None, limit=1, **KW)
    assert [c.value for c in res.claims] == ["200"]
    assert res.coverage.notes == ("limited to the 1 newest records",)


def test_literature_unknown_substance_is_a_gap(mesh):
    res = literature(mesh, 999, None, **KW)
    assert res.claims == ()
    assert res.coverage.missing == ("article_mesh",)


def test_empty_article_mesh_is_a_gap(ref_db):
    res = literature(ref_db, 1, None, **KW)
    assert res.claims == ()
    assert res.coverage.missing == ("article_mesh",)
