"""PubMed relation parsers: each relation reads only its own element path. No DB."""

from __future__ import annotations

from defusedxml.ElementTree import fromstring

from episteme.data.article_schema import MAX_LIST_ELEMENT_BYTES
from episteme.data.graph_files import RELATIONS
from episteme.data.pubmed import relations as rel

SF = "baseline__pubmed26n0001.xml.gz"

RECORD = """
<PubmedArticle>
  <MedlineCitation>
    <PMID Version="1">40571748</PMID>
    <Article>
      <AuthorList>
        <Author><LastName>Doe</LastName><ForeName>Jane</ForeName><Initials>J</Initials>
          <Identifier Source="ORCID">https://orcid.org/0000-0002-1825-009X</Identifier>
          <AffiliationInfo><Affiliation>Dept A, Univ X.</Affiliation></AffiliationInfo>
          <AffiliationInfo><Affiliation>Hospital Y.</Affiliation></AffiliationInfo>
        </Author>
        <Author><CollectiveName>COVID Study Group</CollectiveName></Author>
        <Author><LastName>Roe</LastName><Identifier Source="ORCID">12345</Identifier></Author>
      </AuthorList>
      <PublicationTypeList>
        <PublicationType UI="D016428">Journal Article</PublicationType>
        <PublicationType UI="D016441">Retracted Publication</PublicationType>
      </PublicationTypeList>
    </Article>
    <ChemicalList>
      <Chemical><RegistryNumber>0</RegistryNumber><NameOfSubstance UI="D007328">Insulin</NameOfSubstance></Chemical>
      <Chemical><RegistryNumber>EC 1.1.1.1</RegistryNumber><NameOfSubstance UI="C000123">Foo protein</NameOfSubstance></Chemical>
    </ChemicalList>
    <CommentsCorrectionsList>
      <CommentsCorrections RefType="RetractionIn"><RefSource>J X 2025</RefSource><PMID Version="1">41000001</PMID></CommentsCorrections>
      <CommentsCorrections RefType="CommentOn"><RefSource>J Y 2020</RefSource></CommentsCorrections>
    </CommentsCorrectionsList>
    <MeshHeadingList>
      <MeshHeading><DescriptorName UI="D006801" MajorTopicYN="N">Humans</DescriptorName></MeshHeading>
      <MeshHeading>
        <DescriptorName UI="D003920" MajorTopicYN="N">Diabetes Mellitus</DescriptorName>
        <QualifierName UI="Q000188" MajorTopicYN="Y">drug therapy</QualifierName>
        <QualifierName UI="Q000453" MajorTopicYN="N">epidemiology</QualifierName>
      </MeshHeading>
    </MeshHeadingList>
    <KeywordList Owner="NOTNLM">
      <Keyword MajorTopicYN="N">insulin resistance</Keyword>
    </KeywordList>
  </MedlineCitation>
  <PubmedData>
    <ArticleIdList>
      <ArticleId IdType="pubmed">40571748</ArticleId>
      <ArticleId IdType="doi">10.1007/s00059-025-05325-x</ArticleId>
      <ArticleId IdType="pmc">PMC123</ArticleId>
    </ArticleIdList>
    <ReferenceList>
      <Reference><Citation>Ref one.</Citation>
        <ArticleIdList><ArticleId IdType="pubmed">111</ArticleId><ArticleId IdType="doi">10.1/one</ArticleId></ArticleIdList>
      </Reference>
      <Reference><Citation>Ref two, no ids.</Citation></Reference>
      <ReferenceList><Reference><Citation>Nested.</Citation>
        <ArticleIdList><ArticleId IdType="doi">10.1016/j.lanhl.2024.100671</ArticleId><ArticleId IdType="pmc">PMC999</ArticleId></ArticleIdList>
      </Reference></ReferenceList>
    </ReferenceList>
  </PubmedData>
</PubmedArticle>
"""  # noqa: E501


def _art():
    return fromstring(RECORD)


def test_own_ids_ignore_reference_ids():
    ids = rel.own_article_ids(_art())
    assert ids["doi"] == "10.1007/s00059-025-05325-x"
    assert ids["pmc"] == "PMC123"


def test_every_relation_key_present():
    edges, counters = rel.extract_relations(_art(), "40571748", SF)
    assert tuple(edges) == RELATIONS
    assert counters["edges_skipped_no_pmid"] == 0


def test_mesh_rows():
    edges, _ = rel.extract_relations(_art(), "40571748", SF)
    m = {r["descriptor_ui"]: r for r in edges["mesh"]}
    assert m["D006801"]["major_topic"] is False
    assert m["D003920"]["major_topic"] is True  # a qualifier is major
    assert m["D003920"]["qualifier_uis"] == ["Q000188", "Q000453"]
    assert m["D003920"]["qualifiers"] == ["drug therapy", "epidemiology"]
    assert all(r["pmid"] == "40571748" and r["source_file"] == SF for r in edges["mesh"])


def test_cites_rows_include_unresolved_and_nested():
    edges, _ = rel.extract_relations(_art(), "40571748", SF)
    c = edges["cites"]
    assert [r["dst_pmid"] for r in c] == ["111", None, None]
    assert c[0]["dst_doi"] == "10.1/one"
    assert c[1]["ref_text"] == "Ref two, no ids."
    assert c[2]["dst_doi"] == "10.1016/j.lanhl.2024.100671"
    assert all(r["src_pmid"] == "40571748" for r in c)


def test_comments_chemicals_keywords():
    edges, _ = rel.extract_relations(_art(), "40571748", SF)
    assert [(r["ref_type"], r["ref_pmid"]) for r in edges["comments"]] == [
        ("RetractionIn", "41000001"),
        ("CommentOn", None),
    ]
    assert edges["comments"][1]["ref_text"] == "J Y 2020"
    chem = edges["chemicals"]
    assert (chem[0]["substance_ui"], chem[0]["name"], chem[0]["registry_number"]) == (
        "D007328",
        "Insulin",
        None,  # "0" means no registry number
    )
    assert chem[1]["registry_number"] == "EC 1.1.1.1"
    kw = edges["keywords"][0]
    assert (kw["keyword"], kw["owner"], kw["major_topic"]) == (
        "insulin resistance",
        "NOTNLM",
        False,
    )


def test_authors_orcid_and_affiliations():
    edges, counters = rel.extract_relations(_art(), "40571748", SF)
    a = edges["authors"]
    assert [x["position"] for x in a] == [1, 2, 3]
    assert a[0]["orcid"] == "0000-0002-1825-009X"
    assert a[0]["affiliations"] == ["Dept A, Univ X.", "Hospital Y."]
    assert a[1]["collective_name"] == "COVID Study Group"
    assert a[2]["orcid"] is None
    assert counters["orcid_dropped"] == 1


def test_normalize_orcid():
    assert rel.normalize_orcid("0000-0002-1825-009x") == "0000-0002-1825-009X"
    assert rel.normalize_orcid("http://orcid.org/0000000218250097") == "0000-0002-1825-0097"
    assert rel.normalize_orcid("orcid.org/0000-0002-1825-0097") == "0000-0002-1825-0097"
    assert rel.normalize_orcid("12345") is None
    assert rel.normalize_orcid(None) is None


def test_retracted_rule():
    assert rel.retracted(["Retracted Publication"], []) is True
    assert rel.retracted(["Retraction of Publication"], []) is False
    assert rel.retracted([], [{"ref_type": "RetractionIn"}]) is True
    assert rel.retracted(["Journal Article"], [{"ref_type": "RetractionOf"}]) is False


def test_no_pmid_means_no_edges():
    edges, counters = rel.extract_relations(_art(), "", SF)
    assert all(edges[r] == [] for r in RELATIONS)
    assert counters["edges_skipped_no_pmid"] == 1


def test_long_text_is_capped_including_keywords():
    long = "x" * 5000
    xml = RECORD.replace("insulin resistance", long).replace("Ref two, no ids.", long)
    edges, _ = rel.extract_relations(fromstring(xml), "40571748", SF)
    assert len(edges["keywords"][0]["keyword"].encode()) <= MAX_LIST_ELEMENT_BYTES
    assert len(edges["cites"][1]["ref_text"].encode()) <= MAX_LIST_ELEMENT_BYTES
