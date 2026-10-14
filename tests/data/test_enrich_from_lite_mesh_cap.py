"""enrich_from_lite._mesh_terms caps each term for the GIN index on mesh. No DB."""

from __future__ import annotations


def test_mesh_terms_are_capped_for_the_gin_index():
    from xml.etree import ElementTree as ET

    from episteme.data.article_schema import MAX_LIST_ELEMENT_BYTES
    from episteme.data.europepmc.lite_metadata.enrich_from_lite import _mesh_terms

    long_term = "Neoplasms " * 400
    elem = ET.fromstring(
        "<result><meshHeadingList>"
        "<meshHeading><descriptorName>Humans</descriptorName></meshHeading>"
        f"<meshHeading><descriptorName>{long_term}</descriptorName></meshHeading>"
        "</meshHeadingList></result>"
    )
    terms = _mesh_terms(elem)
    assert terms[0] == "Humans"
    assert len(terms[1].encode("utf-8")) <= MAX_LIST_ELEMENT_BYTES
    assert terms[1].endswith("…")
