"""Schema 1.6 article row: own ids only, retraction rule, edges returned. No DB."""

from __future__ import annotations

from defusedxml.ElementTree import fromstring

from episteme.data.article_schema import SCHEMA_VERSION
from episteme.data.pubmed.extract_pubmed import parse_pubmed_article, parse_pubmed_record

from .test_pubmed_relations import RECORD, SF


def test_schema_is_1_6():
    assert SCHEMA_VERSION == "1.6"


def test_doi_and_pmcid_come_from_the_records_own_id_list():
    row = parse_pubmed_article(fromstring(RECORD), SF)
    assert row["doi"] == "10.1007/s00059-025-05325-x"
    assert row["pmcid"] == "PMC123"


def test_retraction_notice_is_not_retracted():
    notice = RECORD.replace("Retracted Publication", "Retraction of Publication").replace(
        'RefType="RetractionIn"', 'RefType="RetractionOf"'
    )
    assert parse_pubmed_article(fromstring(notice), SF)["is_retracted"] is False
    assert parse_pubmed_article(fromstring(RECORD), SF)["is_retracted"] is True


def test_retraction_in_alone_marks_retracted():
    only_link = RECORD.replace(
        '<PublicationType UI="D016441">Retracted Publication</PublicationType>', ""
    )
    assert parse_pubmed_article(fromstring(only_link), SF)["is_retracted"] is True


def test_record_returns_row_edges_counters():
    row, edges, counters = parse_pubmed_record(fromstring(RECORD), SF)
    assert row["pmid"] == "40571748"
    assert len(edges["mesh"]) == 2
    assert counters["orcid_dropped"] == 1
    assert row["mesh"] == ["Humans", "Diabetes Mellitus"]  # article column unchanged
